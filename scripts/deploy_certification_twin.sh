#!/usr/bin/env bash
#
# Deploy the ALREADY-BUILT candidate image to the private certification twin.
#
# This script never builds. That is the point, not a convenience: the twin has
# to run the byte-identical artifact staged for production, and a rebuild --
# even from the same commit -- produces a different digest and therefore
# certifies a different artifact. It accepts one digest and refuses anything
# that is not one.
#
# It is also not a production deployment. The twin is IAM-private, carries no
# traffic, and runs as a service account with no mailbox, send, queue, or
# production-data authority. Ordinary `process-user` staging is Baylor's
# command and is not performed here.
#
#   scripts/deploy_certification_twin.sh --dry-run   # default: prints, runs nothing
#   scripts/deploy_certification_twin.sh --apply
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

SERVICE="process-user-certification"
REGION="${CERTIFICATION_REGION:-us-central1}"
PROJECT="${CERTIFICATION_PROJECT:-email-automation-cache}"
RUNTIME_SA="sitesift-certification-runtime@${PROJECT}.iam.gserviceaccount.com"
OPERATOR_SA="sitesift-certification-operator@${PROJECT}.iam.gserviceaccount.com"

mode="dry-run"
case "${1:-}" in
  ""|--dry-run) ;;
  --apply) mode="apply" ;;
  *) printf 'Usage: %s [--dry-run|--apply]\n' "$0" >&2; exit 64 ;;
esac

fail() { printf 'REFUSED: %s\n' "$1" >&2; exit "${2:-65}"; }

# --- the checkout must be exactly what was reviewed ------------------------
#
# A stamp names a revision. With uncommitted changes, the revision it names is
# not the code in the image.
[[ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]] \
  || fail "deployment checkout must be clean" 67

SOURCE_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ "$SOURCE_REVISION" =~ ^[0-9a-f]{40}$ ]] \
  || fail "git HEAD did not resolve to a full 40-character SHA" 68

# The reviewed source must already be public, or nobody else can rebuild or
# reproduce what is about to be certified. The agent never pushes; Baylor does.
UPSTREAM_REVISION="$(git -C "$REPO_ROOT" rev-parse '@{u}' 2>/dev/null || true)"
[[ "$UPSTREAM_REVISION" == "$SOURCE_REVISION" ]] \
  || fail "local HEAD is not pushed; Baylor pushes, this script never does" 69

# --- the image must be an existing digest, never a tag ---------------------
: "${TESTED_IMAGE_DIGEST:?set TESTED_IMAGE_DIGEST to the exact repository@sha256 already built and tested}"
[[ "$TESTED_IMAGE_DIGEST" =~ ^[^[:space:]]+@sha256:[0-9a-f]{64}$ ]] \
  || fail "TESTED_IMAGE_DIGEST must be repository@sha256:<64 hex>, never a tag" 70

IMAGE_DIGEST="${TESTED_IMAGE_DIGEST##*@}"

: "${FIXTURE_CONFIG_SECRET_VERSION:?set FIXTURE_CONFIG_SECRET_VERSION to the exact numeric secret version}"
# `latest` is an alias that can be repointed after review; `0` is not a version;
# `07` is a second spelling of the same number, which would give one deployment
# two spellings of its own identity.
[[ "$FIXTURE_CONFIG_SECRET_VERSION" =~ ^[1-9][0-9]*$ ]] \
  || fail "FIXTURE_CONFIG_SECRET_VERSION must be a positive decimal, not an alias" 71

: "${CERTIFICATION_OPERATOR_SUB:?set CERTIFICATION_OPERATOR_SUB to the operator service account numeric uniqueId}"
[[ "$CERTIFICATION_OPERATOR_SUB" =~ ^[0-9]+$ ]] \
  || fail "CERTIFICATION_OPERATOR_SUB must be the numeric uniqueId" 72

: "${PRODUCTION_CANDIDATE_REVISION:?set PRODUCTION_CANDIDATE_REVISION to the staged candidate revision name}"

SERVICE_URL="https://${SERVICE}-PROJECTHASH-uc.a.run.app"
if [[ -n "${CERTIFICATION_SERVICE_URL:-}" ]]; then
  SERVICE_URL="$CERTIFICATION_SERVICE_URL"
fi

deploy_command=(
  gcloud run deploy "$SERVICE"
  --project "$PROJECT"
  --region "$REGION"
  --image "$TESTED_IMAGE_DIGEST"
  --service-account "$RUNTIME_SA"
  --ingress internal
  --no-allow-unauthenticated
  --concurrency 1
  --max-instances 1
  --timeout 540
  --command gunicorn
  --args "--bind=:8080,--workers=1,--threads=8,--max-requests=1,--timeout=0,service:app"
  --set-env-vars "K_SERVICE=${SERVICE},SITESIFT_SOURCE_REVISION=${SOURCE_REVISION},SITESIFT_IMAGE_DIGEST=${IMAGE_DIGEST},SITESIFT_PRODUCTION_CANDIDATE_REVISION=${PRODUCTION_CANDIDATE_REVISION},FIRESTORE_DATABASE=sitesift-certification,SITESIFT_FIXTURE_CONFIG_SECRET_VERSION=${FIXTURE_CONFIG_SECRET_VERSION},SITESIFT_CERTIFICATION_AUDIENCE=${SERVICE_URL},SITESIFT_CERTIFICATION_OPERATOR_EMAIL=${OPERATOR_SA},SITESIFT_CERTIFICATION_OPERATOR_SUB=${CERTIFICATION_OPERATOR_SUB}"
  --set-secrets "CERTIFICATION_FIXTURE_CONFIG=sitesift-certification-fixture-config:${FIXTURE_CONFIG_SECRET_VERSION}"
  # No traffic flag: a twin is never a traffic target, and this service has no
  # production traffic to split in the first place.
)

# Only the operator may invoke it. Not allUsers, not allAuthenticatedUsers, and
# not a human's own token -- the stamp binds the operator's identity, and a
# direct user token would bind the wrong one.
invoker_command=(
  gcloud run services add-iam-policy-binding "$SERVICE"
  --project "$PROJECT"
  --region "$REGION"
  --member "serviceAccount:${OPERATOR_SA}"
  --role roles/run.invoker
)

printf 'service           %s\n' "$SERVICE"
printf 'project/region    %s / %s\n' "$PROJECT" "$REGION"
printf 'image (digest)    %s\n' "$TESTED_IMAGE_DIGEST"
printf 'source revision   %s\n' "$SOURCE_REVISION"
printf 'runtime SA        %s\n' "$RUNTIME_SA"
printf 'fixture secret    sitesift-certification-fixture-config:%s\n' "$FIXTURE_CONFIG_SECRET_VERSION"
printf 'ingress           internal, no unauthenticated invokers\n'
printf '\n'

if [[ "$mode" == "dry-run" ]]; then
  printf 'DRY RUN. Nothing was executed. Commands that --apply would run:\n\n'
  printf '  %q ' "${deploy_command[@]}"; printf '\n\n'
  printf '  %q ' "${invoker_command[@]}"; printf '\n'
  exit 0
fi

"${deploy_command[@]}"
"${invoker_command[@]}"

printf '\nDeployed. Verify before certifying anything:\n'
printf '  gcloud run services describe %s --project %s --region %s --format yaml\n' \
  "$SERVICE" "$PROJECT" "$REGION"
