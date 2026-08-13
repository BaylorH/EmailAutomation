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
short_sha="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)"
if [[ ! "$short_sha" =~ ^[0-9a-f]{12}$ ]]; then
  printf 'Refusing: git HEAD did not resolve to a 12-character lowercase SHA.\n' >&2
  exit 68
fi
image_tag="${IMAGE_REPOSITORY}:${short_sha}"
revision_suffix="stage-${short_sha}"
candidate_revision="${SERVICE}-${revision_suffix}"

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
service_describe_command=(
  gcloud run services describe "$SERVICE"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  --format=json
)

env_vars="^:^FIREBASE_BUCKET=email-automation-cache.firebasestorage.app:ENFORCE_OPENAI_BUDGET=1:USAGE_MONTHLY_BUDGET_USD=100:SITESIFT_AUTO_REPLY_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2:SITESIFT_DAILY_SEND_CAP=20:SITESIFT_GLOBAL_DAILY_SEND_CAP=20:SITESIFT_TOUR_ACTION_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2:SITESIFT_OUTBOUND_MODE=live"
secrets="AZURE_API_APP_ID=AZURE_API_APP_ID:latest,AZURE_API_CLIENT_SECRET=AZURE_API_CLIENT_SECRET:latest,FIREBASE_API_KEY=FIREBASE_API_KEY:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,GOOGLE_OAUTH_CLIENT_ID=GOOGLE_OAUTH_CLIENT_ID:latest,GOOGLE_OAUTH_CLIENT_SECRET=GOOGLE_OAUTH_CLIENT_SECRET:latest,GOOGLE_REFRESH_TOKEN=GOOGLE_REFRESH_TOKEN:latest"

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

if [[ "$mode" == "dry-run" ]]; then
  printf 'dry-run: zero gcloud commands will execute\n'
  printf 'image tag: %s\n' "$image_tag"
  printf 'candidate revision: %s\n' "$candidate_revision"
  printf 'staging contract: untagged at 0%% traffic; existing release-a and positive traffic are not mutated\n'
  print_command "${build_command[@]}"
  print_command "${digest_command[@]}"
  printf 'deploy image after digest resolution: %s@sha256:<64-hex-digest>\n' "$image_tag"
  exit 0
fi

process_user_gcloud_preflight apply

if ! GCLOUD_ACCOUNT="$ACCOUNT" \
    python3 "$SCRIPT_DIR/phase1_rollout.py" --verify-staging-prerequisites; then
  printf 'Refusing to stage: rules, passive UI, switches, or rollback prerequisites are not exact.\n' >&2
  exit 78
fi

if ! baseline_service_json="$("${service_describe_command[@]}")"; then
  printf 'Refusing to stage: baseline service read failed.\n' >&2
  exit 73
fi
if ! baseline_revision="$({
  BASELINE_SERVICE_JSON="$baseline_service_json" \
  CANDIDATE_REVISION="$candidate_revision" \
  python3 - <<'PY'
import json
import os
import re


def refuse(message):
    raise SystemExit(message)


try:
    service = json.loads(os.environ["BASELINE_SERVICE_JSON"])
except (KeyError, json.JSONDecodeError) as error:
    refuse(f"baseline service JSON is invalid: {error}")

status = service.get("status")
status_traffic = status.get("traffic") if isinstance(status, dict) else None
if not isinstance(status_traffic, list):
    refuse("baseline service traffic is missing or invalid")

spec = service.get("spec")
spec_traffic = spec.get("traffic") if isinstance(spec, dict) else None
if not isinstance(spec_traffic, list) or not spec_traffic:
    refuse("baseline spec.traffic is missing or invalid")


def validate_traffic(items, surface):
    positive = {}
    tags = {}
    canonical = []
    for item in items:
        if not isinstance(item, dict):
            refuse(f"{surface} traffic contains a non-object entry")
        if item.get("latestRevision") not in (None, False):
            refuse(f"{surface} traffic contains a LATEST target")
        if "revisionName" not in item:
            refuse(f"{surface} traffic contains an implicit target")
        revision = item.get("revisionName")
        if not isinstance(revision, str) or not revision:
            refuse(f"{surface} traffic does not name an exact revision")
        percent = item.get("percent", 0)
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            refuse(f"{surface} traffic contains an invalid percent")
        if percent < 0 or percent > 100:
            refuse(f"{surface} traffic percent is outside 0..100")
        if percent > 0:
            positive[revision] = positive.get(revision, 0) + percent
        tag = item.get("tag")
        if tag is not None:
            if not isinstance(tag, str) or re.fullmatch(
                r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", tag
            ) is None:
                refuse(f"{surface} traffic contains an invalid tag")
            if tag in tags:
                refuse(f"{surface} traffic duplicates a tag")
            tags[tag] = revision
        canonical.append((revision, percent, tag))
    return positive, tags, sorted(canonical, key=lambda item: (item[2] or "", item[0], item[1]))

spec_positive, spec_tags, _ = validate_traffic(spec_traffic, "baseline spec")
status_positive, status_tags, _ = validate_traffic(status_traffic, "baseline status")

if len(spec_positive) != 1:
    refuse(f"expected exactly one positive baseline spec revision, found {spec_positive!r}")
baseline_revision, baseline_percent = next(iter(spec_positive.items()))
if baseline_percent != 100:
    refuse(f"baseline spec revision traffic is {baseline_percent!r}, not 100")
if status_positive != spec_positive:
    refuse("baseline status traffic does not match explicit spec traffic")

if spec_tags.get("release-a") != baseline_revision or list(spec_tags).count("release-a") != 1:
    refuse(
        "release-a must map exactly once to the sole 100 percent baseline revision"
    )
if status_tags != spec_tags:
    refuse("baseline status tags do not match explicit spec tags")

candidate = os.environ["CANDIDATE_REVISION"]
if status.get("latestCreatedRevisionName") == candidate or any(
    item.get("revisionName") == candidate for item in (*spec_traffic, *status_traffic)
):
    refuse(f"deterministic candidate revision already appears in service state: {candidate}")

annotations = service.get("metadata", {}).get("annotations", {})
if annotations.get("run.googleapis.com/maxScale") != "20":
    refuse("baseline service-wide maxScale is not 20")

print(baseline_revision)
PY
})"; then
  printf 'Refusing to stage: baseline service contract is not exact.\n' >&2
  exit 74
fi

revision_list_command=(
  gcloud run revisions list
  --service "$SERVICE"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  --format=json
)
if ! revision_inventory_json="$("${revision_list_command[@]}")"; then
  printf 'Refusing to stage: revision inventory read failed.\n' >&2
  exit 79
fi
if ! REVISION_INVENTORY_JSON="$revision_inventory_json" \
    CANDIDATE_REVISION="$candidate_revision" \
    python3 - <<'PY'
import json
import os


def refuse(message):
    raise SystemExit(message)


try:
    inventory = json.loads(os.environ["REVISION_INVENTORY_JSON"])
except (KeyError, json.JSONDecodeError) as error:
    refuse(f"revision inventory JSON is invalid: {error}")
if not isinstance(inventory, list):
    refuse("revision inventory is not a list")
names = []
for entry in inventory:
    if not isinstance(entry, dict):
        refuse("revision inventory contains a non-object entry")
    name = entry.get("metadata", {}).get("name")
    if not isinstance(name, str) or not name:
        refuse("revision inventory contains an invalid identity")
    names.append(name)
if len(set(names)) != len(names):
    refuse("revision inventory contains duplicate identities")
if os.environ["CANDIDATE_REVISION"] in names:
    refuse("deterministic candidate revision already exists in full inventory")
PY
then
  printf 'Refusing to stage: revision inventory contract is not exact.\n' >&2
  exit 80
fi

baseline_revision_describe_command=(
  gcloud run revisions describe "$baseline_revision"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  --format=json
)
if ! baseline_revision_json="$("${baseline_revision_describe_command[@]}")"; then
  printf 'Refusing to stage: baseline revision read failed.\n' >&2
  exit 81
fi
if ! BASELINE_REVISION_JSON="$baseline_revision_json" \
    EXPECTED_REVISION="$baseline_revision" \
    python3 - <<'PY'
import json
import os


def refuse(message):
    raise SystemExit(message)


try:
    revision = json.loads(os.environ["BASELINE_REVISION_JSON"])
except (KeyError, json.JSONDecodeError) as error:
    refuse(f"baseline revision JSON is invalid: {error}")
if not isinstance(revision, dict):
    refuse("baseline revision is not a JSON object")
if revision.get("metadata", {}).get("name") != os.environ["EXPECTED_REVISION"]:
    refuse("baseline revision identity does not match the sole positive target")
spec = revision.get("spec")
if not isinstance(spec, dict):
    refuse("baseline revision spec is missing")
containers = spec.get("containers")
if not isinstance(containers, list) or len(containers) != 1:
    refuse("baseline revision must have exactly one container")
if not isinstance(containers[0].get("image"), str) or "@sha256:" not in containers[0]["image"]:
    refuse("baseline revision image is not immutable")
PY
then
  printf 'Refusing to stage: baseline revision contract is not exact.\n' >&2
  exit 82
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
  --revision-suffix "$revision_suffix"
)
"${deploy_command[@]}"

if ! post_service_json="$("${service_describe_command[@]}")"; then
  printf 'Refusing to certify staging: post-deploy service read failed.\n' >&2
  exit 75
fi
if ! BASELINE_SERVICE_JSON="$baseline_service_json" \
    POST_SERVICE_JSON="$post_service_json" \
    BASELINE_REVISION="$baseline_revision" \
    CANDIDATE_REVISION="$candidate_revision" \
    python3 - <<'PY'
import json
import os
import re


def refuse(message):
    raise SystemExit(message)


def load(name):
    try:
        value = json.loads(os.environ[name])
    except (KeyError, json.JSONDecodeError) as error:
        refuse(f"{name} is invalid: {error}")
    if not isinstance(value, dict):
        refuse(f"{name} is not a JSON object")
    return value


def normalized_routes(service):
    status = service.get("status")
    status_traffic = status.get("traffic") if isinstance(status, dict) else None
    spec = service.get("spec")
    spec_traffic = spec.get("traffic") if isinstance(spec, dict) else None
    if not isinstance(status_traffic, list) or not isinstance(spec_traffic, list):
        refuse("service spec or status traffic is missing or invalid")

    def parse(items, surface):
        positive = {}
        tags = {}
        canonical = []
        for item in items:
            if not isinstance(item, dict):
                refuse(f"{surface} traffic contains a non-object entry")
            if item.get("latestRevision") not in (None, False):
                refuse(f"{surface} traffic contains a LATEST target")
            revision = item.get("revisionName")
            if not isinstance(revision, str) or not revision:
                refuse(f"{surface} traffic does not name an exact revision")
            percent = item.get("percent", 0)
            if isinstance(percent, bool) or not isinstance(percent, (int, float)):
                refuse(f"{surface} traffic contains an invalid percent")
            if percent < 0 or percent > 100:
                refuse(f"{surface} traffic percent is outside 0..100")
            if percent > 0:
                positive[revision] = positive.get(revision, 0) + percent
            tag = item.get("tag")
            if tag is not None:
                if not isinstance(tag, str) or re.fullmatch(
                    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", tag
                ) is None:
                    refuse(f"{surface} traffic contains an invalid tag")
                if tag in tags:
                    refuse(f"{surface} traffic duplicates a tag")
                tags[tag] = revision
            canonical.append((revision, percent, tag))
        return positive, tags, sorted(canonical, key=lambda item: (item[2] or "", item[0], item[1]))

    status_positive, status_tags, status_canonical = parse(status_traffic, "status")
    spec_positive, spec_tags, spec_canonical = parse(spec_traffic, "spec")
    if status_positive != spec_positive or status_tags != spec_tags:
        refuse("service status routing does not match explicit spec routing")
    return (
        status,
        status_traffic,
        status_positive,
        status_tags,
        spec_canonical,
        status_canonical,
    )


baseline = load("BASELINE_SERVICE_JSON")
post = load("POST_SERVICE_JSON")
(
    _,
    _,
    baseline_positive,
    baseline_tags,
    baseline_spec,
    baseline_status,
) = normalized_routes(baseline)
(
    post_status,
    post_traffic,
    post_positive,
    post_tags,
    post_spec,
    post_status_routes,
) = normalized_routes(post)
baseline_revision = os.environ["BASELINE_REVISION"]
candidate = os.environ["CANDIDATE_REVISION"]

if post_status.get("latestCreatedRevisionName") != candidate:
    refuse(
        "latestCreatedRevisionName is missing, invalid, or does not identify "
        f"the deterministic candidate: {post_status.get('latestCreatedRevisionName')!r}"
    )
if baseline_positive != {baseline_revision: 100}:
    refuse(f"baseline positive traffic is ambiguous: {baseline_positive!r}")
if post_positive != baseline_positive:
    refuse(
        "positive traffic changed during tagless staging: "
        f"before={baseline_positive!r} after={post_positive!r}"
    )
if baseline_tags.get("release-a") != baseline_revision:
    refuse("baseline release-a mapping is not bound to the sole positive revision")
if post_tags != baseline_tags:
    refuse(
        "traffic tag mapping changed during tagless staging: "
        f"before={baseline_tags!r} after={post_tags!r}"
    )
if post_spec != baseline_spec:
    refuse("canonical spec.traffic changed during tagless staging")
if post_status_routes != baseline_status:
    refuse("canonical status.traffic changed during tagless staging")

candidate_percent = sum(
    item.get("percent", 0)
    for item in post_traffic
    if item.get("revisionName") == candidate
)
if candidate_percent != 0:
    refuse(f"candidate traffic is {candidate_percent!r}, not 0")
if any(item.get("revisionName") == candidate and item.get("tag") for item in post_traffic):
    refuse("candidate unexpectedly has a traffic tag")

baseline_annotations = baseline.get("metadata", {}).get("annotations", {})
post_annotations = post.get("metadata", {}).get("annotations", {})
if post_annotations.get("run.googleapis.com/maxScale") != baseline_annotations.get(
    "run.googleapis.com/maxScale"
):
    refuse("service-wide maxScale changed during staging")
PY
then
  printf 'Refusing to certify staging: service routing readback is not exact.\n' >&2
  exit 76
fi

revision_describe_command=(
  gcloud run revisions describe "$candidate_revision"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  --format=json
)
if ! candidate_revision_json="$("${revision_describe_command[@]}")"; then
  printf 'Refusing to certify staging: candidate revision read failed.\n' >&2
  exit 77
fi
if ! REVISION_JSON="$candidate_revision_json" \
    BASELINE_REVISION_JSON="$baseline_revision_json" \
    EXPECTED_REVISION="$candidate_revision" \
    EXPECTED_IMAGE="${IMAGE_REPOSITORY}@${digest}" \
    EXPECTED_SERVICE_ACCOUNT="$SERVICE_ACCOUNT" \
    python3 - <<'PY'
import json
import os


def refuse(message):
    raise SystemExit(message)


try:
    revision = json.loads(os.environ["REVISION_JSON"])
except (KeyError, json.JSONDecodeError) as error:
    refuse(f"candidate revision JSON is invalid: {error}")
try:
    baseline_revision = json.loads(os.environ["BASELINE_REVISION_JSON"])
except (KeyError, json.JSONDecodeError) as error:
    refuse(f"baseline revision JSON is invalid: {error}")

if revision.get("metadata", {}).get("name") != os.environ["EXPECTED_REVISION"]:
    refuse("candidate revision name does not match the deterministic identity")

annotations = revision.get("metadata", {}).get("annotations", {})
if annotations.get("autoscaling.knative.dev/maxScale") != "10":
    refuse("candidate revision maxScale is not 10")
if annotations.get("autoscaling.knative.dev/minScale") not in (None, "0"):
    refuse("candidate revision minScale is not 0")


def canonical_metadata(document):
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        refuse("revision metadata is missing")
    result = {}
    ignored = {
        "annotations": {"run.googleapis.com/operation-id"},
        "labels": {
            "serving.knative.dev/configurationGeneration",
            "serving.knative.dev/route",
        },
    }
    for field, ignored_keys in ignored.items():
        values = metadata.get(field, {})
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            refuse(f"revision {field} shape is invalid")
        result[field] = {
            key: value for key, value in values.items() if key not in ignored_keys
        }
    return result


if canonical_metadata(revision) != canonical_metadata(baseline_revision):
    refuse("candidate functional revision metadata differs from baseline")

spec = revision.get("spec")
if not isinstance(spec, dict):
    refuse("candidate revision spec is missing")
baseline_spec = baseline_revision.get("spec")
if not isinstance(baseline_spec, dict):
    refuse("baseline revision spec is missing")
if spec.get("serviceAccountName") != os.environ["EXPECTED_SERVICE_ACCOUNT"]:
    refuse("candidate service account does not match")
if spec.get("containerConcurrency") != 1:
    refuse("candidate containerConcurrency is not 1")
if spec.get("timeoutSeconds") not in (540, "540", "540s"):
    refuse("candidate timeout is not 540 seconds")

containers = spec.get("containers")
if not isinstance(containers, list) or len(containers) != 1:
    refuse("candidate must have exactly one container")
container = containers[0]
baseline_containers = baseline_spec.get("containers")
if not isinstance(baseline_containers, list) or len(baseline_containers) != 1:
    refuse("baseline must have exactly one container")
baseline_container = baseline_containers[0]
if container.get("image") != os.environ["EXPECTED_IMAGE"]:
    refuse(f"candidate image is not the expected immutable digest: {container.get('image')!r}")
if container.get("command") != ["gunicorn"]:
    refuse("candidate command does not match")
if container.get("args") != [
    "--bind=:8080",
    "--workers=1",
    "--threads=8",
    "--max-requests=1",
    "--timeout=0",
    "service:app",
]:
    refuse("candidate arguments do not match")
if container.get("resources", {}).get("limits", {}).get("memory") != "2Gi":
    refuse("candidate memory limit is not 2Gi")

env = container.get("env")
if not isinstance(env, list):
    refuse("candidate environment is missing")
by_name = {}
for entry in env:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        refuse("candidate environment contains an invalid entry")
    name = entry["name"]
    if name in by_name:
        refuse(f"candidate environment duplicates {name}")
    by_name[name] = entry

expected_values = {
    "FIREBASE_BUCKET": "email-automation-cache.firebasestorage.app",
    "ENFORCE_OPENAI_BUDGET": "1",
    "USAGE_MONTHLY_BUDGET_USD": "100",
    "SITESIFT_AUTO_REPLY_ALLOWLIST": "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
    "SITESIFT_DAILY_SEND_CAP": "20",
    "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
    "SITESIFT_TOUR_ACTION_ALLOWLIST": "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
    "SITESIFT_OUTBOUND_MODE": "live",
}
for name, value in expected_values.items():
    if by_name.get(name, {}).get("value") != value:
        refuse(f"candidate environment value does not match for {name}")

for name in (
    "AZURE_API_APP_ID",
    "AZURE_API_CLIENT_SECRET",
    "FIREBASE_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
):
    secret_ref = by_name.get(name, {}).get("valueFrom", {}).get("secretKeyRef", {})
    if secret_ref.get("name") != name or secret_ref.get("key") != "latest":
        refuse(f"candidate secret reference does not match for {name}")

def canonical_spec(value):
    value = json.loads(json.dumps(value))
    containers = value.get("containers")
    if isinstance(containers, list) and len(containers) == 1:
        containers[0].pop("image", None)
    return value


if canonical_spec(spec) != canonical_spec(baseline_spec):
    refuse("candidate config differs from the baseline revision beyond immutable image")

ready_conditions = [
    condition
    for condition in revision.get("status", {}).get("conditions", [])
    if isinstance(condition, dict) and condition.get("type") == "Ready"
]
if len(ready_conditions) != 1 or str(ready_conditions[0].get("status")).lower() != "true":
    refuse("candidate Ready condition is not exactly True")
PY
then
  printf 'Refusing to certify staging: candidate image, config, or Ready readback is not exact.\n' >&2
  exit 78
fi

printf 'Verified untagged staging candidate %s from immutable image %s at 0%% traffic; baseline revision %s remains the sole 100%% target and release-a mapping is unchanged.\n' \
  "$candidate_revision" "$immutable_image" "$baseline_revision"
