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

## Schedule it (replaces the '*/30 * * * *' Actions cron)

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
| `AZURE_API_APP_ID` | job env | Non-secret app id (appid-prefix check). |
| `AZURE_API_CLIENT_SECRET`, `FIREBASE_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Secret Manager | Referenced via `secretKeyRef`, never inlined. |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | **Deliberately unset.** ADC via the job SA replaces the Actions `sa.json` file. |

### Lease owner in the new runtime

`scheduler_lease._default_owner()` resolves to, in order:
`GITHUB_RUN_ID` → `RENDER_INSTANCE_ID` → `f"{hostname}:{pid}"`.

On GitHub Actions the owner is the run id (unchanged). In a Cloud Run container
none of those run-id vars are set, so the owner degrades to the stable
`hostname:pid` identity — unique per task container, deterministic within the
process. This fallback **already existed**; `tests/test_scheduler_lease_cloudrun_runtime.py`
pins it so the migration can't regress it.

---

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

**Unverified / gaps (NOT proven — do not assume):**
- **SIGTERM → atexit upload.** `main.py` now installs a SIGTERM handler that calls
  `sys.exit(0)` so atexit handlers (the token-cache upload) run on Cloud Run shutdown.
  This is **reasoned, not end-to-end tested**: importing `main.py` pulls the full
  pipeline (firebase/Graph/OpenAI clients) and requires live-ish env, so a clean unit
  test of the container-shutdown path was not written rather than write a hollow one.
  Validate on GCP by executing the job and confirming a token-cache upload after a
  forced task termination.
- **Image build.** The Dockerfile was not built in this environment (no Docker/`gcloud`
  available; no cloud mutations permitted). Wheel availability for `python:3.12-slim`
  is expected for all requirements but unverified by an actual `docker build`.
- **`gcloud` commands** above are unrun (hard rule: no cloud state mutation). Treat as a
  runbook to execute manually.
- **24h task timeout** — the YAML sets `timeoutSeconds: 3600` (1h) as a safe default,
  not the 24h ceiling; raise it if a real run approaches the limit. The *capability* is
  24h; the configured value is conservative.
