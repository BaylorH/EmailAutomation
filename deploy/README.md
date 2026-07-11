# WS-B — EmailAutomation scheduler → Cloud Run Job (scaffold)

Migrate the scheduler worker off GitHub Actions cron (`.github/workflows/email.yml`
→ `python main.py` every 30 min) onto a **Python Cloud Run Job on Cloud Scheduler**.

Zero application rewrite: the entry point is still `python main.py` (the per-user
pipeline `refresh_and_process_user`, wrapped in the Firestore single-runner lease
`email_automation/scheduler_lease.py`, TTL 45 min). The lease is transport-agnostic
and survives the move unchanged. Auth is via ADC — `firestore.Client()` picks up the
job's service account, so no key file is needed (contrast `email.yml`, which writes a
service-account JSON to `$RUNNER_TEMP/sa.json`).

> **Scaffold only.** Nothing here has been applied to any GCP project. All
> `*_PLACEHOLDER` tokens must be replaced. No real project IDs / buckets / secrets
> are committed.

---

## Files in this migration

| File | Purpose |
|------|---------|
| `../Dockerfile` | Container image: `python:3.12-slim`, installs `requirements.txt`, non-root `appuser`, entrypoint `python main.py`. |
| `cloudrun-job.yaml` | Cloud Run Job spec — task timeout, service-account placeholder, env vars (parameterized bucket + launch-safety scope), Secret Manager references. |
| `cloudrun-service.yaml` | **Phase-1 webhook** Cloud Run *Service* spec — same image, gunicorn entrypoint serving `service.py` (`POST /process-user`), per-user lease, `PROCESS_USER_AUTH` gate. |
| `../service.py` | HTTP entrypoint: wraps `main.refresh_and_process_user` behind `run_with_user_lease`; routes `POST /process-user` + `GET /healthz`. |
| `../email_automation/app_config.py` | `FIREBASE_BUCKET` now reads env, defaults to historical value. |
| `../firebase_helpers.py` | Same env parameterization on the bucket that actually drives the token-cache round-trip. |
| `../main.py` | SIGTERM→`sys.exit` bridge so the atexit token-cache upload runs on container shutdown. |
| `../tests/test_scheduler_lease_cloudrun_runtime.py` | Lease-owner + mutual-exclusion contract for the new runtime. |

---

## One-time setup (replace every PLACEHOLDER)

```bash
PROJECT_ID=your-project-id
REGION=us-central1
AR_REPO=email-automation
SA=email-automation-scheduler@${PROJECT_ID}.iam.gserviceaccount.com

# 1. Artifact Registry repo (once)
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker --location="$REGION"

# 2. Service account (once) + roles: Firestore + Storage + Secret access
gcloud iam service-accounts create email-automation-scheduler
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor"

# 3. Create secrets (values NOT in this repo)
for s in azure-api-client-secret firebase-api-key openai-api-key \
         google-oauth-client-id google-oauth-client-secret google-refresh-token; do
  printf '%s' "REPLACE_ME" | gcloud secrets create "$s" --data-file=- || true
done
```

## Build + deploy the job

```bash
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/email-automation:$(git rev-parse --short HEAD)"

gcloud builds submit --tag "$IMAGE" .            # build from repo root (Dockerfile there)
# edit deploy/cloudrun-job.yaml: set image + all *_PLACEHOLDER values
gcloud run jobs replace deploy/cloudrun-job.yaml --region "$REGION"
```

## Schedule it (replaces the `*/30 * * * *` Actions cron)

```bash
gcloud scheduler jobs create http email-automation-every-30m \
  --location="$REGION" \
  --schedule="*/30 * * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/email-automation-scheduler:run" \
  --http-method=POST \
  --oauth-service-account-email="$SA"
```

## Run once manually (smoke test)

```bash
gcloud run jobs execute email-automation-scheduler --region "$REGION"
```

---

## Env vars

| Var | Source | Notes |
|-----|--------|-------|
| `FIREBASE_BUCKET` | job env (optional) | New: parameterized. Defaults to `email-automation-cache.firebasestorage.app` when unset, so old behavior is preserved. |
| `SITESIFT_DEV_SCOPED_SCHEDULER` / `..._TARGET_USER_IDS` / `..._ALLOWED_USER_IDS` | job env | Launch-safety scope carried over verbatim from `email.yml`. |
| `SITESIFT_SCHEDULER_ALLOW_ALL_USERS` | job env (later) | **Cloud Run is fail-closed** (`scheduler_scope.py`, pinned by `tests/test_scheduler_scope.py`): when `CLOUD_RUN_JOB`/`CLOUD_RUN_EXECUTION` are present and the dev-scope flag is not exactly `'1'`, the run raises `SchedulerScopeError` instead of silently processing all users. When the Baylor/BP21 proof is clean and the job should widen to every user, remove the dev-scope trio AND set this to `'1'` explicitly. A dropped or mistyped scope env can no longer fail open. |
| `AZURE_API_APP_ID` | job env | Non-secret app id. **Hard startup gate** (`main._validate_startup_env`, parity with the legacy 'Validate CLIENT_ID prefix' step): the job exits non-zero before lease acquisition unless it starts with `54cec`. |
| `AZURE_API_CLIENT_SECRET`, `FIREBASE_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Secret Manager | Referenced via `secretKeyRef`, never inlined. |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | **Deliberately unset.** ADC via the job SA replaces the Actions `sa.json` file. |

### Intentionally omitted legacy env vars

The legacy workflow (`.github/workflows/email.yml`) injects three env vars the
job spec deliberately does NOT carry over. They are **unused by the scheduler
path** — `main.py`'s import closure (`main.py` + `email_automation/` +
`firebase_helpers.py`) never reads them. Pinned by
`tests/test_ws_b_secret_coverage_contract.py` (AST scan): if the scheduler code
ever starts reading one of these, that test fails and the job spec must be
updated first. Do not cargo-cult them back in as secrets.

| Legacy var | Why omitted |
|------------|-------------|
| `CLIENT_ID` | Omitted — read only by `noPopup_signin_emails_to_excel.py:19`, a commented-out alternate entry point that never runs. (Distinct from `AZURE_API_APP_ID`, which IS carried over.) |
| `FIREBASE_SA_KEY` | Omitted — read only by `config.py:18`, which serves the Flask `app.py`, not `main.py`. The job authenticates to Firestore via ADC instead. |
| `AZURE_TENANT_ID` | Omitted — read only by `config.py:7`. The scheduler's `AUTHORITY` is hardcoded to `/common` in `email_automation/app_config.py:9`, so the tenant id never reaches the MSAL client. |

### Lease owner in the new runtime

`scheduler_lease._default_owner()` resolves to, in order:
`GITHUB_RUN_ID` → `RENDER_INSTANCE_ID` → `f"{hostname}:{pid}"`.

On GitHub Actions the owner is the run id (unchanged). In a Cloud Run container
none of those run-id vars are set, so the owner degrades to the stable
`hostname:pid` identity — unique per task container, deterministic within the
process. This fallback **already existed**; `tests/test_scheduler_lease_cloudrun_runtime.py`
pins it so the migration can't regress it.

---

## Cutover & rollback

Both schedulers can briefly coexist — the Firestore lease
(`schedulerLeases/emailAutomation`) makes double-triggering safe (one runner
wins, the other skips) — but steady-state must be exactly ONE trigger source.
The legacy workflow file stays in the repo throughout: it is the behavioral
spec and the rollback target. Disable it; never delete it until parity is
proven and rollback is formally retired.

### Cutover (GHA cron → Cloud Scheduler)

```bash
# 1. Smoke-run the Cloud Run job manually; confirm a clean lease
#    acquire→release and a token-cache upload in the logs.
gcloud run jobs execute email-automation-scheduler --region "$REGION"

# 2. Let one scheduled cycle fire and verify the same.
gcloud scheduler jobs describe email-automation-every-30m --location="$REGION"

# 3. Disable the legacy GitHub Actions cron (keep the file in-repo).
gh workflow disable "Email Automation"   # .github/workflows/email.yml

# 4. Verify no further GHA runs appear.
gh run list --workflow=email.yml --limit 3
```

### Rollback (Cloud Scheduler → GHA cron)

```bash
# 1. Stop the new trigger FIRST.
gcloud scheduler jobs pause email-automation-every-30m --location="$REGION"

# 2. If an execution is mid-flight, either let it finish (<= 40 min,
#    timeoutSeconds) or cancel it; a killed task's lease self-heals via the
#    45-min TTL even if release is lost.
gcloud run jobs executions list --job=email-automation-scheduler --region="$REGION"

# 3. Re-enable the legacy GitHub Actions cron.
gh workflow enable "Email Automation"

# 4. Verify the next */30 GHA run goes green.
gh run list --workflow=email.yml --limit 3
```

Resuming later: `gcloud scheduler jobs resume email-automation-every-30m
--location="$REGION"` (and re-run the cutover checklist).

### Concurrency semantics flipped vs GHA — know this before debugging

Legacy GHA used `concurrency: cancel-in-progress: true`, which killed the OLD
run when a new one started; the Firestore lease has the opposite polarity — it
skips the NEW run while the old one holds the lease. Consequence: a hung task
no longer gets cancelled after ~30 min; it lives until `timeoutSeconds`
(2400s), and every scheduler tick in that window logs a lease skip. So
"scheduler skipped" log lines during an incident usually mean a stuck or slow
predecessor, not a broken trigger.

---

## Phase-1 webhook (Cloud Run Service) — `cloudrun-service.yaml`

Alongside the batch JOB, `service.py` exposes the **same per-user pipeline** as an
HTTP endpoint so a queue (Cloud Tasks) can drive one user per request instead of
cron-scanning all users. FUNCTIONALITY-NEUTRAL: the endpoint calls
`main.refresh_and_process_user(uid)` unchanged, wrapped in a **per-user lease**
(`schedulerLeases/emailAutomation:{uid}`, TTL 600s / 10 min — see
`scheduler_lease.DEFAULT_USER_LEASE_TTL_SECONDS`). The global batch lease
(`schedulerLeases/emailAutomation`, 45-min TTL) and `run_with_scheduler_lease`
are untouched; the two lease families never share a doc.

Contract:

| Route | Request | Response |
|-------|---------|----------|
| `POST /process-user` | JSON `{"uid": "<firebase-uid>"}` | `200 {"status":"processed"}` ran · `503 {"status":"skipped_locked"}` same-uid already running (Cloud Tasks retries) · `400` missing/blank uid or non-JSON · `401` auth required + missing/wrong secret · `500 {"error":...}` pipeline raised (Cloud Tasks retries) |
| `GET /healthz` | — | `200` (never auth-gated) |

**Auth.** Optional in-app shared secret via `PROCESS_USER_AUTH`; when set, requests
must send it as `Authorization: Bearer <secret>` or `X-Process-User-Auth: <secret>`
(constant-time compare). When unset the endpoint is open — rely on Cloud Run IAM.
**TODO(auth):** real OIDC ID-token verification for the Cloud Tasks→Cloud Run
invoker is deferred to the platform layer (deploy the service with "require
authentication" and give Cloud Tasks an OIDC token whose audience is the service
URL); the shared secret is the in-app defense-in-depth minimum.

```bash
# Same image as the job; the Service overrides the entrypoint to gunicorn.
gcloud builds submit --tag "$IMAGE" .
printf '%s' "REPLACE_ME" | gcloud secrets create process-user-auth --data-file=- || true
# edit deploy/cloudrun-service.yaml: set image + all *_PLACEHOLDER values
gcloud run services replace deploy/cloudrun-service.yaml --region "$REGION"
```

Pinned by `tests/test_ws_b_cloudrun_service_spec.py` (gunicorn entrypoint, request
timeout <= user-lease TTL, secret gate wired, ADC only, no batch scope trio) and
`tests/test_user_lease.py` + `tests/test_process_user_service.py` (lease + HTTP
contract). **Not built/pushed/deployed** in this environment (same hard rule as
the job scaffold).

## Verified vs Unverified (honest gaps)

**Verified (proven in this scaffold):**
- New lease test passes: `python -m unittest tests.test_scheduler_lease_cloudrun_runtime` → 5/5 OK.
- Existing lease tests still pass unchanged (`tests.test_scheduler_lease`).
- Bucket parameterization is backwards-compatible: `FIREBASE_BUCKET` unset → both
  `app_config` and `firebase_helpers` return the original hardcoded value (unit-checkable,
  pure `os.getenv(..., default)`).
- Lease owner falls back to `hostname:pid` when `GITHUB_RUN_ID` is unset, is
  deterministic within a process, and preserves mutual exclusion between two
  distinct owners (covered by the new test).
- `Dockerfile` and `cloudrun-job.yaml` are syntactically well-formed scaffolds.
- **SIGTERM → atexit bridge** (`main._install_sigterm_atexit_bridge`), pinned by
  `tests/test_sigterm_atexit_bridge.py`: a real subprocess given SIGTERM with the
  bridge installed exits 0 with its atexit handler run (token-cache-upload stand-in),
  while a control child WITHOUT the bridge dies at `-SIGTERM` with the handler never
  fired; and a `SystemExit` raised mid-callback still releases the Firestore lease
  via the `finally` in `run_with_scheduler_lease` (status `released`, not a 45-min
  TTL squat). Local + deterministic; what remains live-only is listed below.

**Unverified / gaps (NOT proven — do not assume):**
- **SIGTERM → token-cache upload on real GCP.** The bridge and lease-release
  mechanics are unit-pinned (above), but the end-to-end path — Cloud Run actually
  delivering SIGTERM within the grace period and the REAL `upload_token` atexit
  handler finishing against Firebase Storage before SIGKILL — still needs one live
  validation: execute the job, force-terminate the task, confirm the upload in logs.
- **Image build.** The Dockerfile was not built in this environment (no Docker/`gcloud`
  available; no cloud mutations permitted). Wheel availability for `python:3.12-slim`
  is expected for all requirements but unverified by an actual `docker build`.
- **`gcloud` commands** above are unrun (hard rule: no cloud state mutation). Treat as a
  runbook to execute manually.
- **Task timeout vs lease TTL (hard invariant)** — the YAML sets
  `timeoutSeconds: 2400` (40m), and it MUST stay `<=` the Firestore lease TTL
  (`scheduler_lease.DEFAULT_TTL_SECONDS` = 2700s / 45m). A task that outlives its
  lease holds an expired lease, so the next Cloud Scheduler trigger acquires it and
  two runners execute concurrently — the double-send scenario the lease prevents
  (legacy GHA relied on cancel-in-progress instead; Cloud Run has no cancel).
  Pinned by `tests/test_ws_b_cloudrun_job_spec.py`. If a real run ever needs more
  than 40m, raise `DEFAULT_TTL_SECONDS` first, then the timeout, keeping
  timeout `<=` TTL.

### Prove rollback and guaranteed Release A restoration

Run this only after the exact `release-a` revision has been promoted to 100%
traffic and its image/readback checks have passed. The block refuses to mutate
traffic otherwise. It restores that same Release A revision on every exit after
traffic mutation, including rollback-command and readback failures.

```bash
set -Eeuo pipefail

PREFLIGHT_HELPER="${PREFLIGHT_HELPER:-$PWD/scripts/process_user_gcloud_preflight.sh}"
REGION="us-central1"
SERVICE="process-user"
REPOSITORY="cloud-run-source-deploy"
ROLLBACK_REVISION="REPLACE_ME_ROLLBACK_REVISION"
EXPECTED_ROLLBACK_IMAGE="REPLACE_ME_ROLLBACK_IMAGE@sha256:REPLACE_ME_ROLLBACK_DIGEST"

if [[ "$ROLLBACK_REVISION" == *REPLACE_ME* || "$EXPECTED_ROLLBACK_IMAGE" == *REPLACE_ME* ]]; then
  printf 'Refusing: replace every REPLACE_ME rollback target before running.\n' >&2
  exit 65
fi
if [[ ! -f "$PREFLIGHT_HELPER" ]]; then
  printf 'Refusing: run from the repository root or set PREFLIGHT_HELPER explicitly.\n' >&2
  exit 66
fi
source "$PREFLIGHT_HELPER"
process_user_gcloud_preflight apply

APPROVED_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
PROJECT="$PROCESS_USER_PROJECT"

short_sha="$(git rev-parse --short=12 HEAD)"
if [[ ! "$short_sha" =~ ^[0-9a-f]{12}$ ]]; then
  printf 'Refusing: git HEAD did not resolve to a 12-character lowercase SHA.\n' >&2
  exit 70
fi
image_tag="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/${SERVICE}:${short_sha}"
image_digest="$(
  gcloud artifacts docker images describe "$image_tag" \
    --account "$APPROVED_ACCOUNT" \
    --project "$PROJECT" \
    '--format=value(image_summary.digest)'
)"
if [[ ! "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'Refusing: Release A image digest readback was invalid.\n' >&2
  exit 71
fi
expected_image="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/${SERVICE}@${image_digest}"

service_json="$(
  gcloud run services describe "$SERVICE" \
    --account "$APPROVED_ACCOUNT" \
    --project "$PROJECT" \
    --region "$REGION" \
    --format=json
)"
release_revision="$(
  SERVICE_JSON="$service_json" python3 - <<'PY'
import json
import os

service = json.loads(os.environ["SERVICE_JSON"])
matches = [
    target.get("revisionName")
    for target in service.get("status", {}).get("traffic", [])
    if target.get("tag") == "release-a"
]
if len(matches) != 1 or not matches[0]:
    raise SystemExit(f"expected exactly one release-a tagged revision, found {matches!r}")
if service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/maxScale") != "20":
    raise SystemExit("service-wide run.googleapis.com/maxScale is not 20")
release_percent = sum(
    target.get("percent", 0)
    for target in service.get("status", {}).get("traffic", [])
    if target.get("revisionName") == matches[0]
)
positive_targets = {
    target.get("revisionName")
    for target in service.get("status", {}).get("traffic", [])
    if target.get("percent", 0) > 0
}
if release_percent != 100 or positive_targets != {matches[0]}:
    raise SystemExit("Release A must already be the sole 100 percent traffic target")
print(matches[0])
PY
)"

revision_json="$(
  gcloud run revisions describe "$release_revision" \
    --account "$APPROVED_ACCOUNT" \
    --project "$PROJECT" \
    --region "$REGION" \
    --format=json
)"
REVISION_JSON="$revision_json" EXPECTED_IMAGE="$expected_image" python3 - <<'PY'
import json
import os

revision = json.loads(os.environ["REVISION_JSON"])
spec = revision.get("spec", {})
containers = spec.get("containers", [])
image = containers[0].get("image", "") if len(containers) == 1 else ""
if image != os.environ["EXPECTED_IMAGE"]:
    raise SystemExit(f"Release A image does not match the built digest: {image!r}")
if spec.get("containerConcurrency") != 1:
    raise SystemExit("Release A containerConcurrency is not 1")
annotations = revision.get("metadata", {}).get("annotations", {})
if annotations.get("autoscaling.knative.dev/maxScale") != "10":
    raise SystemExit("Release A revision maxScale is not 10")
PY

if [[ ! "$EXPECTED_ROLLBACK_IMAGE" =~ ^${REGION}-docker\.pkg\.dev/${PROJECT}/${REPOSITORY}/${SERVICE}@sha256:[0-9a-f]{64}$ ]]; then
  printf 'Refusing: EXPECTED_ROLLBACK_IMAGE must use Cloud Run canonical repository@digest form.\n' >&2
  exit 72
fi
rollback_revision_json="$(
  gcloud run revisions describe "$ROLLBACK_REVISION" \
    --account "$APPROVED_ACCOUNT" \
    --project "$PROJECT" \
    --region "$REGION" \
    --format=json
)"
REVISION_JSON="$rollback_revision_json" EXPECTED_IMAGE="$EXPECTED_ROLLBACK_IMAGE" python3 - <<'PY'
import json
import os

revision = json.loads(os.environ["REVISION_JSON"])
containers = revision.get("spec", {}).get("containers", [])
image = containers[0].get("image", "") if len(containers) == 1 else ""
if image != os.environ["EXPECTED_IMAGE"]:
    raise SystemExit(f"rollback revision image does not match the expected digest: {image!r}")
PY

traffic_revision_at_100() {
  local current_json
  current_json="$(
    gcloud run services describe "$SERVICE" \
      --account "$APPROVED_ACCOUNT" \
      --project "$PROJECT" \
      --region "$REGION" \
      --format=json
  )" || return 1
  SERVICE_JSON="$current_json" python3 - <<'PY'
import json
import os

service = json.loads(os.environ["SERVICE_JSON"])
targets = [
    item.get("revisionName")
    for item in service.get("status", {}).get("traffic", [])
    if item.get("percent", 0) > 0
]
if len(targets) != 1:
    raise SystemExit(f"expected exactly one positive traffic target, found {targets!r}")
percent = next(
    item.get("percent")
    for item in service["status"]["traffic"]
    if item.get("revisionName") == targets[0] and item.get("percent", 0) > 0
)
if percent != 100:
    raise SystemExit(f"traffic target is at {percent!r}, not 100")
print(targets[0])
PY
}

traffic_is_exactly() {
  local expected="$1"
  local actual
  actual="$(traffic_revision_at_100)" || return 1
  [[ "$actual" == "$expected" ]]
}

restore_release_a() {
  local prior_status="$1"
  trap - EXIT
  printf 'Restoring exact Release A revision %s to 100%% traffic...\n' "$release_revision" >&2
  if ! gcloud run services update-traffic "$SERVICE" \
      --account "$APPROVED_ACCOUNT" \
      --project "$PROJECT" \
      --region "$REGION" \
      --to-revisions "${release_revision}=100"; then
    printf 'CRITICAL: Release A traffic restoration command failed.\n' >&2
    return 1
  fi
  if ! traffic_is_exactly "$release_revision"; then
    printf 'CRITICAL: Release A restoration could not be proven by readback.\n' >&2
    return 1
  fi
  printf 'Release A restoration proven at 100%% traffic.\n'
  return "$prior_status"
}

# This trap must be installed before the first rollback traffic mutation.
trap 'restore_release_a $?' EXIT

if ! gcloud run services update-traffic "$SERVICE" \
    --account "$APPROVED_ACCOUNT" \
    --project "$PROJECT" \
    --region "$REGION" \
    --to-revisions "${ROLLBACK_REVISION}=100"; then
  printf 'Rollback traffic mutation failed; EXIT trap will restore Release A.\n' >&2
  exit 1
fi
if ! traffic_is_exactly "$ROLLBACK_REVISION"; then
  printf 'Rollback readback failed; EXIT trap will restore Release A.\n' >&2
  exit 1
fi
printf 'Rollback revision %s proven at 100%% traffic.\n' "$ROLLBACK_REVISION"

restore_release_a 0
```
