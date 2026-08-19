# Certification morning runbook — the four commands only Baylor can run

Written by the overnight session of 2026-08-19. **Nothing in this file was
executed.** Every command here is Baylor-only, because each one either builds a
production artifact, changes a deployed service, or reads raw captured prose.

Read the *Preconditions* block before each command. They are not ceremony: most
of them are enforced by the script itself and will refuse, but a refusal after a
build has already happened costs a rebuild, and a rebuild produces a different
digest — which certifies a different artifact.

---

## Read this before anything else: the SHA moved

The previous handoff said "build the private image **from the pushed SHA**" and
named `0a656fa`. **That SHA is now stale, and building from it would be wrong.**

The overnight session landed a fix to `.dockerignore` that changes *what the
image contains*: `venv/` was anchored at the Docker context root and therefore
never matched `auth_service/venv`, so **2,781 files / ~159 MiB** were being baked
into the image (97.4% of the deployable set). The same root-anchoring bug applied
to the secret patterns — `service-account*.json`, `*credentials*.json`, `.env*`,
`*.pem`, `*.key` — which would have shipped any *nested* copy. No real secret was
actually leaking (none is tracked outside the venv; the `.pem` files that shipped
were public CA bundles), but the rule was wrong and is now `**/`-anchored.

Building from `0a656fa` would ship the old, bloated, un-hardened set and then pin
a digest to it.

**Build from the current pushed HEAD of `feat/native-image-attachment-ingestion-20260816`.**

```bash
cd <worktree>
git fetch origin
git rev-parse HEAD            # must equal
git rev-parse '@{u}'          # this
git status --porcelain        # must be EMPTY
```

If HEAD and upstream differ, push first. `deploy_certification_twin.sh` refuses a
checkout that is not pushed (exit 69) — deliberately, because a stamp names a
revision nobody else can rebuild otherwise.

---

## 1. Build the private candidate image — ONE build, never rebuilt

**Preconditions**

- Clean checkout, HEAD == upstream (above).
- `.dockerignore` fix present. Verify: `grep -n '\*\*/venv/' .dockerignore` returns a line.
- No image has been built for this SHA yet. This build happens **once**; staging
  and the twin both consume the *same* digest. A rebuild from the identical commit
  produces a different digest and therefore certifies a different artifact.

**Command**

```bash
PROJECT_ID=email-automation-cache
REGION=us-central1
AR_REPO=cloud-run-source-deploy

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/process-user:$(git rev-parse --short HEAD)"
gcloud builds submit --tag "$IMAGE" .
```

**Then immediately resolve and record the immutable digest** — everything
downstream takes the digest, never the tag:

```bash
gcloud artifacts docker images describe "$IMAGE" --format='value(image_summary.fully_qualified_digest)'
```

Record that `repository@sha256:...` string. Call it `TESTED_IMAGE_DIGEST`.

**Rollback:** none needed — a built-but-unused image carries no traffic and
changes nothing. Do not delete it; the digest is the evidence anchor. If the build
is wrong, build a new one and simply never reference the bad digest.

**Verify the shrink actually landed** (this is the one number worth eyeballing):

```bash
gcloud artifacts docker images describe "$IMAGE" --format='value(image_summary.image_size_bytes)'
```

Expect it to be roughly 159 MiB smaller than a pre-fix build of the same tree.

---

## 2. Re-run the image-source manifest gate against the REAL image

**This is the half of Task 13 the overnight session could not do**, and it is the
first thing that should happen after the build.

**Preconditions:** step 1 done; `TESTED_IMAGE_DIGEST` in hand.

```bash
# Extract the manifest the image actually carries, then compare it to the checkout.
docker run --rm --entrypoint cat "$TESTED_IMAGE_DIGEST" /app/image_source_manifest.json > /tmp/image-manifest.json
python3 -B scripts/verify_image_source_manifest.py --image-manifest /tmp/image-manifest.json
```

Expect exit `0` and a matching checkout/image digest. A mismatch here means the
deployable set is not what the checkout says it is — **stop and do not deploy**.

**Also close the cross-interpreter matrix in-image.** The overnight session proved
canonical-JSON byte-identity between this host's Python 3.14.3 and a real Python
**3.12.13**, across every fixed vector *and* the refusal surface. What it could
not prove is that the interpreter *inside the image* agrees. One command:

```bash
docker run --rm --entrypoint python "$TESTED_IMAGE_DIGEST" -c 'import sys; assert sys.version_info[:2]==(3,12), sys.version; print(sys.version)'
```

Then run the committed fixed-vector corpus inside the image and confirm the
digests match the pinned constants. Caveat worth knowing: the host comparison
used 3.12.13; the image pins `python:3.12-slim@sha256:423ed6ab...`, whose patch
level may differ. Canonical JSON refuses floats outright — the main cross-runtime
divergence — so a patch difference is very unlikely to matter, but this command is
what turns "unlikely" into "measured".

**Rollback:** none — read-only.

---

## 3. Stage the 0% untagged `process-user` candidate

**Preconditions**

- Steps 1–2 clean.
- Both global campaign switches false.
- Service currently has one sole 100% revision with the single `release-a` mapping.

**This script no longer builds, as of the overnight session.** It used to call
`gcloud builds submit --tag` itself, which meant staging produced a *different
digest* from the one the twin certifies — the certification chain was broken at
its root, because a rebuild from an identical commit yields a different digest and
therefore a different artifact. It now takes the already-built digest as input and
refuses without one.

**Command — dry run FIRST, it executes zero gcloud commands:**

```bash
export TESTED_IMAGE_DIGEST='us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:...'   # from step 1

scripts/deploy_process_user.sh --dry-run
```

It refuses unless `TESTED_IMAGE_DIGEST` is `repository@sha256:<64 lowercase hex>`
— never a tag or alias — names the one expected repository, and is already
present in the registry.

Read the deterministic `process-user-stage-<12-char-HEAD>` revision identity it
reports. Only if that is what you expect:

```bash
scripts/deploy_process_user.sh --apply
```

The script fails closed unless readback proves the candidate is Ready, carries the
exact immutable digest, **remains untagged**, and sits at **0% traffic**, while the
prior revision remains the sole 100% target with every tag unchanged.

**Rollback:** the candidate holds no traffic, so there is nothing to roll back.
Delete the revision if you want it gone. This step does **not** promote, tag,
pause a queue, or mutate traffic — those are separate bounded steps and none of
them are in this runbook.

---

## 4. Deploy the IAM-private certification twin

**Preconditions**

- `TESTED_IMAGE_DIGEST` from step 1 — the script **never builds**, by design.
- Clean checkout AND HEAD == upstream (it refuses otherwise: exits 67 / 69).
- `FIXTURE_CONFIG_SECRET_VERSION` must be a **positive decimal**, never `latest`
  (an alias can be repointed after review, which would give one deployment two
  spellings of its own identity).
- `CERTIFICATION_OPERATOR_SUB` = the operator service account's **numeric uniqueId**.
- `PRODUCTION_CANDIDATE_REVISION` = the revision staged in step 3.

```bash
export TESTED_IMAGE_DIGEST='us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:...'
export FIXTURE_CONFIG_SECRET_VERSION=<positive integer>
export CERTIFICATION_OPERATOR_SUB=<numeric uniqueId>
export PRODUCTION_CANDIDATE_REVISION=process-user-stage-<12-char-HEAD>

scripts/deploy_certification_twin.sh --dry-run      # prints, runs nothing
scripts/deploy_certification_twin.sh --apply
```

The twin deploys `--ingress internal --no-allow-unauthenticated --concurrency 1
--max-instances 1`, with a single `roles/run.invoker` binding to the operator SA.
**It is never a traffic target and carries no traffic flag at all.**

**Verify:**

```bash
gcloud run services describe process-user-certification --project email-automation-cache --region us-central1 --format yaml
```

**Rollback:** `gcloud run services delete process-user-certification`. The twin has
no mailbox, send, queue, or production-data authority, and no traffic, so deleting
it affects nothing else.

> Worth knowing: until tonight, **no test in the repository read this script at
> all.** Three one-token edits — `--allow-unauthenticated`, an `allUsers` invoker,
> `roles/run.admin` — each left the entire suite green. That is now pinned by
> `tests/test_certification_mutation_controls.py`, which parses the real shipped
> script. The commands above are the reviewed shape; if a diff ever shows one of
> those three tokens changed, that is the finding.

---

## 5. The `/review` naturalness command, when a pack reaches `AWAITING_REVIEW`

**This one returns raw captured prose, which is exactly why an agent may never
call it.** `scripts/certify_production.py::assert_agent_may_call` refuses
`review-input` and `review` outright, and that refusal is now driven by test
against the real operation table — so a route added later cannot default to
agent-reachable.

**Preconditions:** a run has reached `AWAITING_REVIEW` (i.e. `review-input`
returns a pack rather than `no_review_pending` 409).

Fetch the ordered pack — `{ordinal, kind, bodyDigest, subject, body}` per message,
whole and ordered from 1, no pagination and no truncation:

```bash
python3 -B scripts/certify_production.py review-input \
  --run-id <run-id> \
  --url "$CERTIFICATION_SERVICE_URL"
```

Read the bodies yourself, decide naturalness against the rubric, then submit the
verdicts. The `review` request body is schema-locked to exactly:

```
{"runId", "expectedRevision", "reviewSetDigest", "rubricVersion", "reviews"}
```

where `reviews` is a list. `reviewSetDigest` comes from the `review-input`
response — it binds your verdicts to the exact pack you actually read, so a pack
that changed underneath you cannot be silently reviewed.

**Rollback:** none — a review is a recorded human judgement, not a mutation of
production. If a verdict is wrong, that is a new review, not an undo.

---

---

## STOP — three blockers now sit between step 4 and any verdict

Found by mutation testing and cross-artifact comparison after this runbook was
first written. **Do not work through steps 1–5 expecting a verdict at the end.**
Each of these fails closed and names its cause, so none is dangerous; together
they mean the twin cannot currently certify anything.

### A. No capability lane is wired — this is the big one

`email_automation/certification/runner.py`:

```python
LANES = {
    "certification-integrity/campaign-one-property": BOOTSTRAP_LANE,
}
```

One entry. That map is what connects a scenario to the product code path it is
supposed to drive. **All 91 capability scenarios return `lane_not_wired`**, and
they would do so even with the twin deployed and the runtime launched. The two
bootstrap/refutation scenarios are the only ones with a lane.

So the certification program can currently prove things about *itself* — that it
runs, isolates, captures, replays, cleans up, and can produce a real FAIL — and
nothing about any product capability. Wiring a lane per capability is unbuilt
work, not a launch you can perform.

### B. The twin will 503 on every `prepare`

`lifecycle.REQUIRED_IDENTITY_ENV` demands seven deployment facts, including
`SITESIFT_FIXTURE_CONFIG_DIGEST`. **`scripts/deploy_certification_twin.sh` never
sets it** (grep count: 0). `_identity()` returns `None` when any one is missing
and `prepare` answers `instrument_unavailable` 503 — correctly, since a missing
fixture-config digest means the stamp would not be bound to the fixture that ran.

Fix before deploying: either have the deploy script compute and set the digest,
or establish why it should not be required. Do not default it — a defaulted
digest binds a stamp to something nobody deployed.

### C. The twin scaffold would have written to the PRODUCTION database

`deploy/cloudrun-certification-service.yaml` never set `FIRESTORE_DATABASE`, so a
twin built from the scaffold would have used the production database rather than
`sitesift-certification`. **Fixed**, and both artifacts are now pinned by
set-equality of env names in both directions. The scaffold is a contract document
that nothing applies — no shipped script references it — so this was never live.
It is recorded because the scaffold is what a human reads to learn what the twin
is, and it was teaching two wrong things at once.

---

## Known blocker between steps 4 and 5 — your call, ~30 seconds

**The twin contract comparator will currently refuse every twin that
`deploy_certification_twin.sh` produces.**

`twin_contract.TWIN_ONLY` classifies exactly three twin-only environment
variables:

```python
TWIN_ONLY = ("K_SERVICE", "FIRESTORE_DATABASE", "CERTIFICATION_FIXTURE_CONFIG")
```

The twin deploy script sets **five more** that the comparator does not classify:

- `SITESIFT_PRODUCTION_CANDIDATE_REVISION`
- `SITESIFT_FIXTURE_CONFIG_SECRET_VERSION`
- `SITESIFT_CERTIFICATION_AUDIENCE`
- `SITESIFT_CERTIFICATION_OPERATOR_EMAIL`
- `SITESIFT_CERTIFICATION_OPERATOR_SUB`

Each will be reported as *"exists only on the twin and is unclassified"*.

**This fails CLOSED and names every field, which is the correct behaviour** — an
unreviewed asymmetric difference should refuse, not be waved through. It is
blocking, not dangerous.

It was deliberately **not** fixed overnight. Widening an allowlist is exactly the
change the plan requires to happen in a reviewed successor, together with its
hostile tests — "approved differences get PAIRED, never deleted", and an
allowlist quietly widened at 4am by an agent is the opposite of that. The locked
matrix does contemplate a certification route/audience, so this reads as a
matrix/implementation gap rather than a bug in either file.

**Your decision:** classify those five in `TWIN_ONLY` (with paired hostile tests
asserting each must be present on the twin and absent on the candidate), or amend
the matrix. Either way it is a reviewed change, not a silent one.

Related and smaller: `deploy/README.md` around lines 301–305 still describes the
staging script as building (*"It then builds and resolves an immutable image
digest"*). That is now false — see step 3. The runbook tests over that README
still pass, so nothing is red; the prose is just stale.

---

## What is still blocked after all five

- **Every capability stamp.** All 91 capability scenarios are `launchClass:
  user_runtime` and need you to launch the product runtime. Only 2 of 93 scenarios
  are `agent_safe`, and both are certification-integrity scenarios that stamp
  nothing (`capabilityStamp: false`). No agent can produce a capability stamp, and
  a guard now refuses all 91 on both the CLI and server paths before any provider
  call.
- **`#91`**, blocked on `#84` (reviewed, remote `b400ee5a`, never integrated — the
  real fix for import-time client construction in `clients.py`).
- **Merging, promoting, traffic changes, Hosting/shared-Functions deploys.** None
  of these appear in this runbook on purpose.
