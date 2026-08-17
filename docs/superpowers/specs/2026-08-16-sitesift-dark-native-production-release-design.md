# SiteSift Dark-Native Production Release Design

**Status:** Approved on 2026-08-16. The user explicitly asked to update
production, summarize what is proved live, identify remaining examination, and
then said “proceed.”

**Deliverable:** both code and findings.

## Release packet

- Reviewed source candidate before this release-boundary change:
  `3956eea7ff81b4aafe4882dd3092e1b8f08505cc`.
- Release branch:
  `feat/native-image-attachment-ingestion-20260816`.
- Pinned live rollback revision:
  `process-user-stage-9491133f15d5`.
- Pinned live rollback image:
  `us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968`.
- Production native-image setting:
  `SITESIFT_NATIVE_IMAGE_INGESTION=false`.

`3956eea7` is the reviewed starting point, not the eventual deployment SHA.
The gate, release-controller, tests, and documentation create a new exact HEAD.
That final HEAD must receive fresh offline verification, independent review,
push/readback parity, an immutable Artifact Registry digest, and Cloud Run
revision readback before it can become the production candidate.

## Problem

The branch contains a deeply reviewed native JPG/PNG attachment path alongside
scanned-PDF, predecessor-processing, retryability, and attachment-snapshot
fixes that should reach production. Native-image behavior, however, still has a
known production-coverage constraint: binding succeeds only when the existing
Property Address cell already contains street, suffix, city, state, and ZIP.
The common split Property Address plus City row remains quarantined by design.
No provider or mailbox canary has proved the native path against a real inbound
message, and an unsafe mailbox harness is out of scope.

Deploying the entire candidate with native behavior active would conflate two
claims:

1. the reviewed non-native fixes are ready to become the latest production
   source; and
2. native-image effects are ready for general production traffic.

Only the first claim is currently supported. The release therefore needs a
dark boundary that preserves the candidate and its non-native improvements
while preventing native bytes from reaching validation, model, Drive, Sheets,
or reply effects.

## Considered approaches

### 1. Exact fail-closed source gate plus exact release readback — selected

Add one configuration predicate that enables native ingestion only when
`SITESIFT_NATIVE_IMAGE_INGESTION` is exactly the lowercase string `true`.
Place the check in `fetch_and_process_pdfs()` after the shared Graph snapshot
has been projected into the existing PDF batch and before any native-image
validation or manifest construction. Deploy the candidate with the explicit
value `false`, then make both staging and promotion validators require exactly
one plain environment entry with that name and value.

This approach preserves PDFs, preserves the single bounded Graph snapshot,
adds no new external call, makes unset/malformed values fail closed, and lets
control-plane readbacks prove that production is dark. It is the smallest
reversible boundary that separates “code present” from “effect enabled.”

### 2. Rebuild a PDF-only branch by dropping native-image commits — rejected

The native milestone was delivered through many small security and integration
commits that also refined shared attachment snapshots, predecessor state, and
retryability. Selectively removing those commits would produce a new,
less-reviewed history and risk losing non-native correctness fixes. It would
also make eventual native enablement a difficult reintegration rather than one
auditable configuration change.

### 3. Deploy native enabled and rely on a live mailbox/provider canary — rejected

This would expose the unresolved address-coverage boundary to ordinary traffic
and require an external message/provider exercise to justify the change. A
health request cannot prove attachment binding, model interpretation, Drive
hosting, or sheet mutation, and this release has no authorized safe harness
that can prove all of those effects without creating misleading production
evidence. Absence of an error is not evidence that native images work.

## Architecture

### Configuration boundary

`email_automation/app_config.py` owns a pure predicate:

```python
def native_image_ingestion_enabled():
    return os.getenv("SITESIFT_NATIVE_IMAGE_INGESTION") == "true"
```

Only exact lowercase `true` enables the feature. Unset, empty, `false`, `TRUE`,
whitespace-padded values, and every other string disable it. The production
release deliberately binds exact lowercase `false`; relying only on absence is
not sufficient because the staged revision must expose an affirmative,
auditable dark-state readback.

### Attachment assembly boundary

`email_automation/file_handling.py` keeps the existing bounded, ordered Graph
attachment snapshot and PDF projection. It processes the PDF entries first.
When the predicate is false, it returns those PDF entries in snapshot order
without invoking native validation or building a native success/failure
manifest. When true, the already reviewed native path runs unchanged.

The existing discriminator that prevents a native-looking attachment with a
PDF MIME claim from falling through into PDF processing remains in force even
while the feature is dark. Disabling native behavior must never reinterpret
image bytes as a PDF.

### Deployment boundary

`scripts/deploy_process_user.sh` adds
`SITESIFT_NATIVE_IMAGE_INGESTION=false` through the existing
`--update-env-vars` contract. It still:

- builds the clean exact repository HEAD;
- resolves and deploys an immutable digest;
- creates deterministic `process-user-stage-<12-character-HEAD>`;
- uses `--no-traffic` and no candidate tag;
- preserves the prior sole 100% revision, `release-a`, and all auxiliary tags;
  and
- reads the candidate back before claiming staging success.

Candidate validation permits one and only one functional difference from the
pinned rollback revision besides image identity: an exact plain environment
entry `{"name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "false"}`.
The baseline must not already contain that entry. Missing, duplicated,
secret-bound, extra-keyed, or nonexact values fail staging and promotion.
Every other inherited environment, secret reference, command, resource,
service-account, metadata, scaling, timeout, IAM, traffic, and queue property
must remain exact.

### Promotion and rollback boundary

`scripts/phase1_rollout.py` is rebound to the release branch and the pinned
rollback revision/image above. The staged candidate remains immutable,
untagged, Ready, and at 0% until the controller:

1. proves source/upstream/remote parity and the exact cloud prerequisites;
2. acquires and repeatedly reasserts the durable rollout lock;
3. pauses and drains the queue;
4. temporarily tags only the candidate for one authenticated `GET /health`;
5. removes that tag and proves its removal;
6. revalidates candidate image, exact false gate, rollback pair, switches,
   topology, IAM, and queue state;
7. promotes the candidate to sole 100% traffic plus `release-a`; and
8. resumes the queue only after post-promotion readbacks pass.

The health request is a container/control-plane proof only. It is not a
provider, mailbox, image-ingestion, PDF-extraction, sheet-write, or reply
canary. The report must say `provider-canary=not-run`.

If promotion may have changed traffic and a later assertion fails, the existing
controller restores `process-user-stage-9491133f15d5` at 100% with `release-a`
and resumes only after exact rollback readbacks. If queue or lock state cannot
be proved, it stops in manual recovery instead of claiming containment. The
separately pinned rollback-proof block in `deploy/README.md` is a maintenance
drill that restores the promoted release on exit; it is not an incident
containment command and is not run as a celebratory post-release traffic drill.
A live rollback mutation occurs only through controller cleanup on a release
failure or through a separately reviewed incident/maintenance decision.

## Data and effect flow

With the gate false:

1. Graph returns the same bounded current-message attachment snapshot already
   required for PDFs.
2. PDF attachments follow their existing extraction, scanned-file fallback,
   Drive archival/preview, model, and downstream handling.
3. Native JPG/PNG candidates are not decoded, normalized, converted to a model
   image input, hosted to Drive, written to Property Image columns, or converted
   into a native quarantine/reply manifest.
4. Mixed PDF/native messages retain the PDF manifests and ignore the native
   members. Image-only messages expose no native manifest, matching the dark
   pre-feature boundary.
5. No new provider, mailbox, Firestore, Sheets, Drive, OpenAI, or browser call is
   introduced by the gate itself.

With the gate true, the reviewed native path is reachable, but production will
not use that state in this release. Enabling it later requires a separate
reviewed release that resolves or explicitly accepts the complete-address
coverage boundary and gathers suitable evidence.

## Privacy and reporting

Tests use synthetic addresses, filenames, bytes, revisions, and responses. No
mailbox content, recipient, token, credential, attachment filename, property
address, model prompt, or sheet value enters release evidence. Cloud log reads
must project only timestamp, severity, revision identity, log name, and request
status/latency metadata; they must not print text payloads.

The post-release report uses five evidence classes:

1. **Proved offline:** deterministic unit/integration/release-controller tests.
2. **Proved live by control plane:** exact revision, immutable digest, false
   gate, readiness, routing, IAM, queue, switches, and authenticated health.
3. **Observed in routine live operation:** only facts supported by sanitized
   production metrics or existing release receipts; never inferred from tests.
4. **Dormant and unproved live:** native image ingestion and every downstream
   native effect.
5. **Needs more examination:** coverage gaps or behavior that has neither a
   deterministic proof nor suitable routine-live evidence.

“Code deployed” and “behavior proved live” are separate statements throughout.

## Error handling

- A nonexact feature value disables native ingestion; it does not raise and
  does not fall open.
- PDF failures preserve their existing retryable/fail-closed semantics.
- A malformed Graph page still fails before partial PDF/native processing.
- Missing, duplicated, or nonexact candidate gate readback aborts staging or
  promotion.
- Any branch, upstream, remote, digest, rollback, routing, IAM, queue, task,
  switch, health, or lock mismatch stops at the existing closed boundary.
- Any failure after a possible traffic change invokes rollback; unprovable
  rollback or queue state becomes explicit manual recovery.
- Authentication refresh failure stops before build, stage, or promotion. The
  operator reauthenticates interactively outside the automated release record
  and restarts from the read-only gates.

## Verification strategy

The implementation follows RED/GREEN commits:

- exact configuration parsing fails before the predicate exists;
- disabled mixed and image-only assembly fails before the file-handling gate;
- controller tests fail until the branch, rollback pair, and exact false
  candidate delta are pinned;
- tagless/production deployment tests fail until the deploy command and
  candidate readback require false; and
- rollback runbook tests fail until the exact live rollback pair replaces the
  former unbound values.

After GREEN, run focused gate tests, the four release contract modules, retained
native/PDF/predecessor tests, broad broker-language and Jill/AI regressions,
dual-Python compilation where available, shell syntax, `git diff --check`,
scope checks, two independent reviews, and a fresh final matrix. Only then push
the exact branch and prove local/upstream/remote parity.

## Rollout and evidence sequence

1. Complete and independently review the nine-path implementation delta.
2. Push the exact final HEAD; do not create a PR or send a review/comment.
3. Refresh the approved gcloud credential through a no-output access-token
   read. Failure is a hard stop.
4. Run both dry-runs and verify they perform no cloud mutation.
5. Stage the immutable candidate untagged at 0%.
6. Independently inspect candidate/rollback/config/traffic readbacks. The false
   gate is mandatory and native remains unproved.
7. Run the closed promotion controller. Do not call either POST route or any
   provider/mailbox harness.
8. Re-read the service, candidate, rollback, queue, tasks, switches, IAM, health,
   and sanitized error/request metadata.
9. Publish the evidence-classified standing report and the examination list.

## What this release may establish

If every gate passes, this release may establish that the latest reviewed
source is running at 100%, the native image path is present but exactly dark,
the service is healthy and privately routed, the queue is running under its
closed contract, and the pinned prior release remains a verified rollback
target. It may also carry forward earlier live evidence for PDFs, broker reply
handling, sheet updates, and safety controls when that evidence exists in prior
receipts or sanitized routine-operation records.

It may not establish that native images bind correctly in live mail, that image
vision extracts facts correctly, that native hosting or Property Image writes
work live, or that a provider send occurred. Those remain explicitly unproved.

## Follow-up examination areas

- Measure authoritative full-address coverage in existing Property Address
  cells before considering native enablement.
- Design and test a safe split Property Address plus City binding strategy
  without filename/body/geocoder rescue.
- Exercise native Graph pagination, real attachment metadata, model vision,
  Drive hosting, and Property Image writes only through a separately authorized,
  bounded test plan.
- Expand scanned-PDF routine-live evidence across long image-only files, mixed
  text/image files, and facts appearing after preview page three.
- Continue the separate natural-English broker-response milestone without
  coupling it to this release.
- Run an emulator-controlled broad regression pass that avoids known dead-
  emulator leakage in legacy tests.
- Schedule a dedicated rollback drill only in a maintenance window if a real
  traffic-switch proof is desired; do not manufacture that evidence during
  this release.

## Refutation conditions

Stop, roll back if traffic changed, and redesign if any of the following occurs:

- any native manifest, decode, model image, Drive upload, sheet image write, or
  native warning/reply effect occurs while the gate is false;
- a value other than exact lowercase `true` enables native ingestion;
- a staged or promoted candidate lacks exactly one plain lowercase-false gate;
- candidate configuration differs from rollback beyond image identity and that
  single gate entry;
- PDFs disappear, reorder, or change behavior in a mixed message while dark;
- the final HEAD, upstream, remote branch, artifact digest, or revision identity
  cannot be proved equal to the release packet;
- the rollback revision/digest differs from the pinned pair;
- staging assigns a tag or positive traffic to the candidate;
- promotion observes queue tasks, enabled campaign switches, public/changed
  IAM, an unexpected health result, lock ambiguity, or routing drift; or
- the final report labels native behavior or a provider/mailbox effect as
  live-proven.
