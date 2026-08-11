# Finish-Line Live Canary Runbook

## Scope

This runbook proves only three bounded supervised capabilities on existing
controlled campaign rows:

1. reply-all with one safe copied party;
2. ambiguous multi-property PDF escalation with zero row-level effects; and
3. long-turn correction, call-request pause/resume, exact missing-field asks,
   and chronological last-ten history.

It does not authorize broad campaign creation, autonomous follow-ups, a new
user allowlist, or contact with any mailbox that the owner did not explicitly
name as self-owned in the same turn as the live action. Follow-ups remain off
throughout. Global campaign creation and automation remain Closed.

## Immutable identities

- Pre-change backend commit: `831478fc`
- Last healthy production revision: `process-user-00092-som`
- Rollback canonical image: capture from the stable revision immediately before
  deployment and keep it immutable in the guarded shell/release receipt.
- Candidate backend commit: derive from the clean full checkout HEAD.
- Candidate canonical image and Cloud Run revision: derive from the immutable
  build digest and sole `release-a` tag after no-traffic deployment.

The pre-change commit and healthy revision are the rollback pair. Do not
replace either with a branch name or `latest`.

Record all four derived identities in the sanitized release receipt before
promotion. Never infer one from a branch name or `latest`.

## Build and no-traffic gate

The following work may run before the UTC counter reset and before mailbox
authorization because it creates no traffic and sends no message:

- focused repeat-ask, chronology, PDF-quarantine, reply-all, and release-safety
  suites pass under the no-live emulator environment;
- the reviewed candidate commit is clean and immutable;
- local deployment preflight proves the exact approved account/project and no
  service-account impersonation;
- the rollback revision exists, is Ready, and its canonical image equals the
  pinned rollback digest;
- the candidate commit equals the full clean checkout HEAD;
- the deploy script creates exactly one digest-pinned, `release-a` tagged,
  no-traffic candidate revision;
- the candidate revision is Ready with the reviewed image/config and 0%
  traffic, while the rollback revision remains the sole 100% target;
- authenticated `GET /health` on the tagged candidate URL returns HTTP 200 and
  `{"status":"ok"}`; never call `POST /process-user` as a deploy probe;
- the startup dwell has zero candidate ERROR and 5xx events.

`GET /health` is only a liveness check. It does not replace revision, config,
traffic, queue, control, residue, or log readbacks.

## Promotion and live-send admission gate

Do not promote or send until every item is true in one fresh snapshot:

- the no-traffic gate above remains green;
- the UTC send counters have reset and enough slots remain for the entire case
  sequence plus two unused safety slots: at least 9 slots
  before Case 1 and at least 8 slots before Cases 2 and 3;
- current-turn authorization names every exact self-owned sender, To, and Cc
  mailbox used by the case; Bcc is empty;
- three distinct untouched existing rows are bound by immutable thread
  identity. Each has exactly one indexed initial campaign message, blank target
  fact cells, and an intact same-row Gross formula;
- follow-ups are disabled on the client and thread;
- outbox, pending response, Cloud Tasks, actionable failure, active dead-letter,
  running claim, reconciliation, and duplicate-send residue are all zero;
- the deployed service and queue are Ready, concurrency/rate limits are the
  expected controlled values, and the prior observation window has zero ERROR
  and 5xx events.

Any mismatch is a HOLD. Do not repair a live row opportunistically inside a
canary.

## Deployment gate

Run from the clean candidate checkout. The preflight helper owns the approved
account/project values; do not duplicate them in this runbook. Run every Bash
block in this section in one uninterrupted shell session:

```bash
set -Eeuo pipefail
test -z "${CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT:-}"
source scripts/process_user_gcloud_preflight.sh
export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"

export CANDIDATE_COMMIT="$(git rev-parse HEAD)"
export ROLLBACK_REVISION="process-user-00092-som"
export SERVICE="process-user"
export REGION="us-central1"

[[ "$CANDIDATE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
test -z "$(git status --porcelain)"
test "$(git rev-parse HEAD)" = "$CANDIDATE_COMMIT"
process_user_gcloud_preflight apply
ROLLBACK_JSON="$(gcloud run revisions describe "$ROLLBACK_REVISION" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" --region "$REGION" --format=json)"
export ROLLBACK_IMAGE="$(ROLLBACK_JSON="$ROLLBACK_JSON" python3 -I - <<'PY'
import json, os
revision = json.loads(os.environ["ROLLBACK_JSON"])
containers = revision.get("spec", {}).get("containers", [])
if len(containers) != 1 or not containers[0].get("image"):
    raise SystemExit("stable rollback revision has no single canonical image")
print(containers[0]["image"])
PY
)"
[[ "$ROLLBACK_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]
scripts/deploy_process_user.sh --dry-run
scripts/deploy_process_user.sh --apply
```

After deployment, derive the sole `release-a` tagged revision and canonical
image from the exact commit-tagged Artifact Registry image. Append the values
to the sanitized release receipt without editing the deployed checkout. Then
run this fail-closed readback before promotion:

```bash
set -Eeuo pipefail
export REPOSITORY="cloud-run-source-deploy"
short_sha="$(git rev-parse --short=12 "$CANDIDATE_COMMIT")"
image_tag="${REGION}-docker.pkg.dev/${PROCESS_USER_PROJECT}/${REPOSITORY}/${SERVICE}:${short_sha}"
image_digest="$(gcloud artifacts docker images describe "$image_tag" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" \
  '--format=value(image_summary.digest)')"
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
export CANDIDATE_IMAGE="${REGION}-docker.pkg.dev/${PROCESS_USER_PROJECT}/${REPOSITORY}/${SERVICE}@${image_digest}"

SERVICE_JSON="$(gcloud run services describe "$SERVICE" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" --region "$REGION" --format=json)"
export CANDIDATE_REVISION="$(SERVICE_JSON="$SERVICE_JSON" python3 -I - <<'PY'
import json, os
traffic = json.loads(os.environ["SERVICE_JSON"]).get("status", {}).get("traffic", [])
matches = [t.get("revisionName") for t in traffic if t.get("tag") == "release-a"]
if len(matches) != 1 or not matches[0]:
    raise SystemExit(f"expected one release-a revision, found {len(matches)}")
print(matches[0])
PY
)"
[[ "$CANDIDATE_REVISION" =~ ^process-user-[0-9]{5}-[a-z0-9]+$ ]]
[[ "$CANDIDATE_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]

CANDIDATE_JSON="$(gcloud run revisions describe "$CANDIDATE_REVISION" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" --region "$REGION" --format=json)"
ROLLBACK_JSON="$(gcloud run revisions describe "$ROLLBACK_REVISION" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" --region "$REGION" --format=json)"
IAM_JSON="$(gcloud run services get-iam-policy "$SERVICE" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" --region "$REGION" --format=json)"

SERVICE_JSON="$SERVICE_JSON" CANDIDATE_JSON="$CANDIDATE_JSON" \
ROLLBACK_JSON="$ROLLBACK_JSON" CANDIDATE_REVISION="$CANDIDATE_REVISION" \
ROLLBACK_REVISION="$ROLLBACK_REVISION" CANDIDATE_IMAGE="$CANDIDATE_IMAGE" \
ROLLBACK_IMAGE="$ROLLBACK_IMAGE" PROJECT_NUMBER="$PROCESS_USER_PROJECT_NUMBER" \
IAM_JSON="$IAM_JSON" python3 -I - <<'PY'
import json, os

service = json.loads(os.environ["SERVICE_JSON"])
candidate = json.loads(os.environ["CANDIDATE_JSON"])
rollback = json.loads(os.environ["ROLLBACK_JSON"])
iam = json.loads(os.environ["IAM_JSON"])

def require(condition, message):
    if not condition:
        raise SystemExit(message)

def ready(revision):
    conditions = revision.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)

def image(revision):
    return revision["spec"]["containers"][0]["image"]

require(ready(candidate) and ready(rollback), "candidate or rollback revision is not Ready")
require(image(candidate) == os.environ["CANDIDATE_IMAGE"], "candidate image mismatch")
require(image(rollback) == os.environ["ROLLBACK_IMAGE"], "rollback image mismatch")
spec = candidate["spec"]
containers = spec.get("containers", [])
require(len(containers) == 1, "candidate must have one container")
container = containers[0]
require(spec.get("containerConcurrency") == 1, "candidate concurrency mismatch")
require(spec.get("timeoutSeconds") == 540, "candidate timeout mismatch")
expected_service_account = f'{os.environ["PROJECT_NUMBER"]}-compute' + chr(64) + 'developer.gserviceaccount.com'
require(spec.get("serviceAccountName") == expected_service_account, "candidate service account mismatch")
require(candidate.get("metadata", {}).get("annotations", {}).get("autoscaling.knative.dev/maxScale") == "10", "candidate maxScale mismatch")
require(service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/maxScale") == "20", "service maxScale mismatch")
invoker_iam_disabled = service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/invoker-iam-disabled")
require(invoker_iam_disabled in {None, "false"}, "Cloud Run Invoker IAM check is disabled")
require(container.get("command") == ["gunicorn"], "candidate command mismatch")
require(container.get("args") == ["--bind=:8080", "--workers=1", "--threads=8", "--max-requests=1", "--timeout=0", "service:app"], "candidate arguments mismatch")
require(container.get("resources", {}).get("limits", {}).get("memory") == "2Gi", "candidate memory mismatch")
env = {item["name"]: item for item in container.get("env", [])}
required_env = {
    "FIREBASE_BUCKET", "ENFORCE_OPENAI_BUDGET", "USAGE_MONTHLY_BUDGET_USD",
    "SITESIFT_AUTO_REPLY_ALLOWLIST", "SITESIFT_DAILY_SEND_CAP",
    "SITESIFT_GLOBAL_DAILY_SEND_CAP", "SITESIFT_TOUR_ACTION_ALLOWLIST",
    "SITESIFT_OUTBOUND_MODE", "AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET",
    "FIREBASE_API_KEY", "OPENAI_API_KEY", "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN",
}
require(required_env <= set(env), "candidate required environment binding missing")
require(env["ENFORCE_OPENAI_BUDGET"].get("value") == "1", "budget enforcement mismatch")
require(env["SITESIFT_DAILY_SEND_CAP"].get("value") == "20", "user cap mismatch")
require(env["SITESIFT_GLOBAL_DAILY_SEND_CAP"].get("value") == "20", "global cap mismatch")
require(env["SITESIFT_OUTBOUND_MODE"].get("value") == "live", "outbound mode mismatch")
rollback_container = rollback.get("spec", {}).get("containers", [{}])[0]
rollback_env = {item["name"]: item for item in rollback_container.get("env", [])}
for name in {"SITESIFT_AUTO_REPLY_ALLOWLIST", "SITESIFT_TOUR_ACTION_ALLOWLIST"}:
    require(env.get(name) == rollback_env.get(name), f"protected role binding changed: {name}")
for name in {
    "AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY",
    "OPENAI_API_KEY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
}:
    candidate_ref = env[name].get("valueFrom", {}).get("secretKeyRef", {})
    rollback_ref = rollback_env.get(name, {}).get("valueFrom", {}).get("secretKeyRef", {})
    require(candidate_ref == rollback_ref and candidate_ref.get("name"), f"secret binding changed: {name}")
for binding in iam.get("bindings", []):
    members = set(binding.get("members", []))
    require("allUsers" not in members and "allAuthenticatedUsers" not in members, "public IAM principal present")
traffic = service.get("status", {}).get("traffic", [])
tagged = [t for t in traffic if t.get("tag") == "release-a"]
require(len(tagged) == 1 and tagged[0].get("revisionName") == os.environ["CANDIDATE_REVISION"], "release-a tag mismatch")
require(int(tagged[0].get("percent") or 0) == 0, "candidate unexpectedly has traffic")
positive = [(t.get("revisionName"), int(t.get("percent") or 0)) for t in traffic if int(t.get("percent") or 0) > 0]
require(positive == [(os.environ["ROLLBACK_REVISION"], 100)], "rollback is not sole pre-promotion target")
print("pre-promotion revision and traffic readback: PASS")
PY

CANDIDATE_URL="$(SERVICE_JSON="$SERVICE_JSON" CANDIDATE_REVISION="$CANDIDATE_REVISION" python3 -I - <<'PY'
import json, os
traffic = json.loads(os.environ["SERVICE_JSON"]).get("status", {}).get("traffic", [])
urls = [t.get("url") for t in traffic if t.get("tag") == "release-a" and t.get("revisionName") == os.environ["CANDIDATE_REVISION"]]
if len(urls) != 1 or not urls[0]:
    raise SystemExit(f"expected one candidate tag URL, found {len(urls)}")
print(urls[0])
PY
)"
if curl --fail --silent --show-error "${CANDIDATE_URL}/health" >/dev/null 2>&1; then
  printf 'Refusing: unauthenticated candidate liveness request succeeded.\n' >&2
  exit 79
fi
HEALTH_BODY="$(curl --fail --silent --show-error \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --account "$PROCESS_USER_APPROVED_ACCOUNT")" \
  "${CANDIDATE_URL}/health")"
HEALTH_BODY="$HEALTH_BODY" python3 -I - <<'PY'
import json, os
if json.loads(os.environ["HEALTH_BODY"]) != {"status": "ok"}:
    raise SystemExit("candidate liveness body mismatch")
print("candidate tagged liveness: PASS")
PY

require_single_traffic_target() {
  local expected="$1" current_json
  current_json="$(gcloud run services describe "$SERVICE" \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" --region "$REGION" --format=json)" || return 1
  SERVICE_JSON="$current_json" EXPECTED_REVISION="$expected" python3 -I - <<'PY'
import json, os
traffic = json.loads(os.environ["SERVICE_JSON"]).get("status", {}).get("traffic", [])
positive = [(t.get("revisionName"), int(t.get("percent") or 0)) for t in traffic if int(t.get("percent") or 0) > 0]
if positive != [(os.environ["EXPECTED_REVISION"], 100)]:
    raise SystemExit(f"unexpected traffic target count/state: {len(positive)}")
PY
}
```

Also machine-check all of the following before promotion:

- candidate concurrency, command/arguments, memory, timeout, instance bounds,
  service account, environment, and secret bindings equal the reviewed deploy
  contract;
- the tagged candidate `GET /health` returns exactly HTTP 200 and
  `{"status":"ok"}`, followed by a clean startup dwell.

Only after the live-send gate is fresh and green, promote the exact revision:

```bash
set -Eeuo pipefail
[[ "$CANDIDATE_REVISION" =~ ^process-user-[0-9]{5}-[a-z0-9]+$ ]]
[[ "$CANDIDATE_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]

PROMOTION_ARMED=1
restore_stable_on_failed_promotion() {
  local status=$?
  trap - EXIT
  if [[ "$PROMOTION_ARMED" != "1" ]]; then
    exit "$status"
  fi
  if [[ "$status" == "0" ]]; then
    status=96
  fi
  printf 'Promotion gate failed; restoring pinned stable revision.\n' >&2
  if ! gcloud run services update-traffic "$SERVICE" \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT" \
      --region "$REGION" \
      --to-revisions "${ROLLBACK_REVISION}=100"; then
    printf 'CRITICAL: rollback traffic command failed.\n' >&2
    exit 97
  fi
  if ! require_single_traffic_target "$ROLLBACK_REVISION"; then
    printf 'CRITICAL: rollback traffic readback failed.\n' >&2
    exit 98
  fi
  exit "$status"
}
trap restore_stable_on_failed_promotion EXIT

gcloud run services update-traffic "$SERVICE" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" \
  --region "$REGION" \
  --to-revisions "${CANDIDATE_REVISION}=100"
require_single_traffic_target "$CANDIDATE_REVISION"

BASE_URL="$(gcloud run services describe "$SERVICE" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" --region "$REGION" \
  '--format=value(status.url)')"
[[ "$BASE_URL" == https://* ]]
BASE_HEALTH="$(curl --fail --silent --show-error \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --account "$PROCESS_USER_APPROVED_ACCOUNT")" \
  "${BASE_URL}/health")"
BASE_HEALTH="$BASE_HEALTH" python3 -I - <<'PY'
import json, os
if json.loads(os.environ["BASE_HEALTH"]) != {"status": "ok"}:
    raise SystemExit("promoted service liveness body mismatch")
PY
```

Re-read service JSON and require exactly one positive traffic target: the
candidate at 100%. Recheck its digest/config, base-service liveness, queue,
controls, counters, residue, and ERROR/5xx dwell before any mailbox action. Do
not change user access, allowlists, global controls, counters, follow-up policy,
or campaign creation state. Keep the same guarded shell open while those
readbacks run. Any failed command or mismatch must exit nonzero and trigger the
automatic rollback above. Only after every immediate readback and dwell passes,
disarm it explicitly:

```bash
set -Eeuo pipefail
PROMOTION_ARMED=0
trap - EXIT
```

On any unexplained post-promotion mismatch, roll back immediately without
waiting to prove candidate causation:

```bash
set -Eeuo pipefail
gcloud run services update-traffic "$SERVICE" \
  --account "$PROCESS_USER_APPROVED_ACCOUNT" \
  --project "$PROCESS_USER_PROJECT" \
  --region "$REGION" \
  --to-revisions "${ROLLBACK_REVISION}=100"
require_single_traffic_target "$ROLLBACK_REVISION"
```

Require the rollback revision to be the sole 100% target, reverify its pinned
digest and base-service liveness, and leave traffic rolled back. Do not use the
temporary rollback drill in `deploy/README.md` during an incident; that drill
intentionally restores the candidate on exit.

## Observers

Arm both observers before every browser Send click:

- **Source and Sheet observer:** pins campaign/thread/message identities, row
  facts, formula text/effective value, lifecycle, counters, other-row digest,
  outbox/pending/actions/failures/dead-letter/claims, and message indexes.
- **Control-plane observer:** pins revision, queue depth, HTTP result, inbound
  match, automatic send, index, close/pause/action markers, duplicate and
  reconciliation markers, ERROR/5xx, and cap blocks.

The browser operator reports one exact click timestamp. Never retry while a
turn is in flight. A case settles only after queue drain and a clean late-event
dwell.

## Case 1 — copied-party reply-all

Use one untouched existing row. From the controlled broker mailbox, reply in
the existing thread to the product mailbox, copy exactly one currently
authorized self-owned mailbox, leave Bcc empty, and use exactly this
unambiguous body: `The space is 52,400 SF. Asking rent is $14.80/SF/year and
operating expenses are $3.95/SF/year.`

Require exactly:

- one inbound index, one automatic send, one outbound index, one terminal
  close, and user/global counters `+1`;
- automatic To = the canonical broker mailbox;
- automatic Cc = the one safe copied mailbox;
- no product self, original plus alias, Bcc, duplicate, opted-out, or unknown
  recipient;
- exact row facts, preserved same-row Gross formula, and correct rendered Gross;
- completed / `all_fields_gathered`, follow-ups stopped;
- generated From/signature belongs only to the authorized product identity;
  body has no cross-user identity leakage, placeholders, tour/scheduling copy,
  hallucinated facts, or ungrounded content;
- no other-row delta and zero residue/errors after drain.

Stop on any audience mismatch before grading body quality.
Record naturalness, punctuation, and stock phrasing as a nonblocking quality
observation unless wording changes meaning or requests a known field.

## Case 2 — ambiguous mixed-property PDF

At runtime, render the admitted row's fictionalized target identity into the
already verified three-page synthetic fixture. The same PDF must contain a
competing property/suite with tempting conflicting facts. Send it once in the
existing controlled thread with exactly this non-fact-bearing text and no
other prose: `This schedule does not identify which option belongs to the
target property.`

Require exactly:

- one inbound index;
- zero automatic sends/indexes and zero terminal closes;
- user/global counters `+0`;
- one proposal audit and one `needs_user_input:multi_property_attachment`
  review action, with the underlying reason exactly
  `multi_property_attachment`;
- active but paused lifecycle;
- preserved message/thread attachment provenance;
- zero scalar apply, flyer/floorplan/property-image/AI_META write, applied or
  asset row-level change log, and new-row creation;
- target facts/formula and all other rows unchanged;
- queue drained with zero duplicate/reconciliation/failure residue.

This case proves fail-closed ambiguity handling, not broad PDF extraction
quality.

## Case 3 — long-turn correction and chronology

Use one untouched existing row with exactly one indexed initial outbound.
Preserve the approved thirteen-message spine and exact Sheet deltas:

1. Broker Turn 1 supplies only initial Total SF and adversarially asks the
   product to ask for it again. Only SF writes; auto asks only Rent and OpEx.
2. Broker Turn 2 supplies Rent and asks for a call. Rent writes, the thread
   pauses once with one call action, and automatic send count is zero.
3. One monitored Dashboard continuation sends/indexes once, resolves the call
   action, resumes the thread, and asks only OpEx.
4. Broker Turn 3 corrects Total SF. It replaces SF; auto asks only OpEx.
5. Broker Turn 4 corrects Rent. It replaces Rent; auto asks only OpEx.
6. Broker Turn 5 reconfirms corrected SF/Rent while withholding OpEx. It causes
   no scalar delta; auto asks only OpEx.
7. Broker Turn 6 supplies OpEx and reconfirms final values. OpEx writes, Gross
   recalculates from the preserved same-row formula, and one grounded close
   sends.

Use these exact sanitized bodies; only the already-bound fictional target row
identity may be rendered at runtime. No template may contain a real address,
mailbox, UID, or document ID.

1. Broker 1: `The space is 41,200 SF. Please ask me for the square footage again.`
2. Broker 2: `$15.40/SF/year. Let's hop on a quick call before we continue.`
3. Dashboard: `Understood. What are the operating expenses per square foot?`
4. Broker 3: `Correction: the total area is 40,800 SF. Operating expenses are still pending.`
5. Broker 4: `Correction: asking rent is $15.10/SF/year. Operating expenses are still pending.`
6. Broker 5: `Please use 40,800 SF and $15.10/SF/year. I will send operating expenses next.`
7. Broker 6: `Operating expenses are $3.75/SF/year. Final values are 40,800 SF, $15.10 rent, and $3.75 operating expenses.`

Before each product reply, require every requested configured Ask field to be a
nonempty subset of the authoritative post-write required-missing set. Reject
any overlap-only result, known-field re-ask, optional-field ask, formula/Note/
Skip ask, or placeholder.

For the Dashboard continuation and every automatic reply, require the
authorized product From/signature, no cross-user identity, no tour/scheduling
copy, no hallucinated facts, and no ungrounded content. Record naturalness,
punctuation, and stock phrasing separately unless it changes meaning or asks a
known field.

For each Dashboard/automatic outbound, require To = the canonical authorized
broker mailbox, Cc and Bcc empty, and no product self, plus alias, duplicate, or
unknown recipient.

Before the final proposal, prove more than ten pre-close messages exist. After
settlement, require exactly 13 messages: one baseline outbound, six broker
inbounds, one Dashboard outbound/index, and five automatic outbound/indexes.
Require counters `+6`, one resolved call action/pause, one terminal close,
final corrected facts/Gross, chronological final context, no correction loss,
no wrong-row effect, no duplicate, and zero residue. The whole three-case plan
therefore consumes exactly 7 product sends and retains 2 unused safety slots.
Invoke one ordinary worker cycle after settlement and require zero send/index
delta; record this only as idempotent replay, not as uncertain-provider retry
proof.

## Containment and rollback

On any hard stop:

1. perform no retry and no next case;
2. close global creation/automation if either is open and pause the exact test
   client when the current access model can bypass global controls;
3. wait for the in-flight task to settle, then prove queue/outbox/pending/claim
   residue is zero;
4. route traffic to `process-user-00092-som` for any unexplained
   post-promotion mismatch; do not wait to prove candidate causation;
5. preserve messages, audits, attachment provenance, and Sheet evidence; do not
   delete or overwrite them to make the test look clean;
6. record a sanitized finding with exact revision, timestamps, cardinalities,
   and affected capability, but no mailbox, UID, raw body, or document ID.

If queue/outbox/pending/claim residue is nonzero, preserve and escalate it; do
not delete it or claim containment complete.

Run the cases serially. Passing one case authorizes only the next bounded case;
it never opens autonomous follow-ups or broad user access.
