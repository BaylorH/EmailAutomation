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
