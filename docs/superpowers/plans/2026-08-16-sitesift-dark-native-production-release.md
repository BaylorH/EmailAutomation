# SiteSift Dark-Native Production Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the latest reviewed SiteSift candidate into production while an
exact fail-closed configuration gate keeps every native-image effect dormant,
then report what is proved live separately from what remains unproved.

**Architecture:** Add an exact `true`-only predicate in application config and
return the already processed PDF projection before native validation when the
predicate is false. Stage the final clean HEAD as an immutable, untagged, 0%
Cloud Run revision with an explicit lowercase-false binding; both the tagless
stager and the closed promotion controller must prove that exact gate as the
only candidate-versus-rollback config delta. Promote only through the existing
locked queue/traffic controller, whose rollback target is pinned to the current
live revision and digest. After removing its temporary health tag, the
controller re-reads the candidate, rollback, switches, topology, IAM, paused
queue, and empty tasks between fresh lock assertions; independent stage and
post-live readbacks keep release evidence separate from controller output.

**Tech Stack:** Python 3, `unittest`, `unittest.mock`, Bash, Git, Google Cloud
CLI, Cloud Build, Artifact Registry, Cloud Run, Cloud Tasks, Firestore REST,
and the existing release-controller test fakes.

---

## Safety and evidence rules

- Implementation starts from branch
  `feat/native-image-attachment-ingestion-20260816`, with reviewed source commit
  `3956eea7ff81b4aafe4882dd3092e1b8f08505cc` in its history.
- The final candidate is the exact post-implementation HEAD, not `3956eea7`.
- The rollback pair is exactly
  `process-user-stage-9491133f15d5` and
  `us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968`.
- Production must contain exactly one plain environment entry
  `SITESIFT_NATIVE_IMAGE_INGESTION=false`. Only exact lowercase `true` enables
  native ingestion in application code.
- No live provider/mailbox canary, POST worker invocation, synthetic mailbox
  message, live attachment, sheet mutation, browser action, PR, comment, or
  external message is part of this plan. Offline tests use synthetic objects
  only.
- Native ingestion remains dormant and unproved live after a successful release.
- Never print access or identity tokens, secret values, mailbox content,
  attachment names, property addresses, model prompts, or sheet values.
- Use `apply_patch` for every source/document edit. Never amend a commit and
  never force-push.
- If any live refutation condition fires before traffic changes, stop. If the
  promotion controller may have changed traffic, let its fenced cleanup restore
  the pinned rollback revision. Do not improvise a traffic command.

## Exact implementation path map

The implementation delta after this plan commit is limited to these nine paths:

1. `email_automation/app_config.py` — exact `true`-only feature predicate.
2. `email_automation/file_handling.py` — PDF-preserving early return before the
   native validation/manifest boundary.
3. `scripts/phase1_rollout.py` — release branch, rollback pair, exact candidate
   dark-gate validation, and locked paused post-tag promotion authorization.
4. `scripts/deploy_process_user.sh` — explicit false deploy binding and exact
   staged-revision readback.
5. `deploy/README.md` — current release packet, dark-state contract, evidence
   limits, and pinned rollback-proof block.
6. `tests/test_image_attachment_ingestion.py` — config semantics, disabled
   mixed/image-only behavior, and explicit enablement for existing native tests.
7. `tests/test_process_user_phase1_rollout_contract.py` — controller release
   packet, exact candidate config-delta, and post-tag call-order/failure tests.
8. `tests/test_process_user_tagless_staging_contract.py` — deploy command,
   candidate readback, and malformed-gate rejection tests.
9. `tests/test_process_user_production_deploy_contract.py` — production env and
   exact rollback-runbook contract.

The design and this plan are planning inputs, not additional implementation
paths.

### Task 1: Prove the implementation starts from the reviewed candidate

**Files:** verification only.

- [ ] **Step 1: Record the exact local state.**

  Run:

  ```bash
  git status --short --branch
  git rev-parse HEAD
  git rev-parse 3956eea7ff81b4aafe4882dd3092e1b8f08505cc
  git merge-base --is-ancestor \
    3956eea7ff81b4aafe4882dd3092e1b8f08505cc HEAD
  git branch --show-current
  ```

  Expected: clean status, the reviewed SHA resolves, the ancestor command exits
  0, and the branch is exactly
  `feat/native-image-attachment-ingestion-20260816`.

- [ ] **Step 2: Resolve the planning boundary for later scope checks.**

  Run:

  ```bash
  PLANNING_COMMIT="$(git log -1 --format=%H -- \
    docs/superpowers/plans/2026-08-16-sitesift-dark-native-production-release.md)"
  test -n "$PLANNING_COMMIT"
  printf '%s\n' "$PLANNING_COMMIT"
  ```

  Expected: one 40-character lowercase commit SHA and exit 0.

### Task 2: Pin exact configuration parsing with a RED commit

**Files:**

- Modify: `tests/test_image_attachment_ingestion.py`
- Test: `tests/test_image_attachment_ingestion.py`

- [ ] **Step 1: Import the configuration module.**

  Add this beside the existing EmailAutomation imports:

  ```python
  from email_automation import app_config
  ```

- [ ] **Step 2: Add the exact fail-closed parsing test.**

  Insert this after the test helpers and before the native validation classes:

  ```python
  class NativeImageReleaseGateTests(unittest.TestCase):
      def test_native_image_gate_enables_only_exact_lowercase_true(self):
          cases = (
              (None, False),
              ("", False),
              ("false", False),
              ("False", False),
              ("TRUE", False),
              (" true", False),
              ("true ", False),
              ("1", False),
              ("true", True),
          )

          for raw_value, expected in cases:
              with self.subTest(raw_value=raw_value):
                  with mock.patch.dict(os.environ, {}, clear=False):
                      os.environ.pop(
                          "SITESIFT_NATIVE_IMAGE_INGESTION",
                          None,
                      )
                      if raw_value is not None:
                          os.environ[
                              "SITESIFT_NATIVE_IMAGE_INGESTION"
                          ] = raw_value
                      self.assertIs(
                          app_config.native_image_ingestion_enabled(),
                          expected,
                      )
  ```

- [ ] **Step 3: Run the one test and prove RED.**

  Run:

  ```bash
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest -v \
      tests.test_image_attachment_ingestion.NativeImageReleaseGateTests.test_native_image_gate_enables_only_exact_lowercase_true
  ```

  Expected: FAIL/ERROR with
  `AttributeError: module 'email_automation.app_config' has no attribute 'native_image_ingestion_enabled'`.

- [ ] **Step 4: Commit only the RED test.**

  ```bash
  git add tests/test_image_attachment_ingestion.py
  git commit -m "test: pin fail-closed native image release gate"
  ```

  Expected: one new non-amended commit; the focused test remains RED.

### Task 3: Implement the exact application gate

**Files:**

- Modify: `email_automation/app_config.py`
- Test: `tests/test_image_attachment_ingestion.py`

- [ ] **Step 1: Add the pure predicate.**

  Add immediately after `E2E_TEST_MODE`:

  ```python
  def native_image_ingestion_enabled():
      """Enable native image effects only for the exact reviewed value."""
      return os.getenv("SITESIFT_NATIVE_IMAGE_INGESTION") == "true"
  ```

- [ ] **Step 2: Re-run the exact parsing test.**

  Run the Task 2 test command again.

  Expected: `OK`; all nine subcases pass.

- [ ] **Step 3: Compile and inspect the minimal delta.**

  ```bash
  python3 -m py_compile \
    email_automation/app_config.py \
    tests/test_image_attachment_ingestion.py
  git diff --check
  git diff -- email_automation/app_config.py \
    tests/test_image_attachment_ingestion.py
  ```

  Expected: compile and whitespace checks exit 0; the production delta is only
  the exact predicate.

- [ ] **Step 4: Commit GREEN.**

  ```bash
  git add email_automation/app_config.py \
    tests/test_image_attachment_ingestion.py
  git commit -m "feat: add fail-closed native image gate"
  ```

### Task 4: Pin the PDF-preserving dark boundary with a RED commit

**Files:**

- Modify: `tests/test_image_attachment_ingestion.py`
- Test: `tests/test_image_attachment_ingestion.py`

- [ ] **Step 1: Add disabled mixed and image-only assembly coverage.**

  Add this method to `NativeImageReleaseGateTests`:

  ```python
      def test_disabled_gate_preserves_pdfs_and_never_enters_native_pipeline(self):
          native_attachment = _attachment(
              "123 Sample Road Example City AZ 85001 exterior.png",
              "image/png",
              _png_bytes(),
          )
          raw_pdf = {
              "name": "broker.pdf",
              "bytes": b"%PDF-1.7 synthetic",
              "_snapshot_index": 1,
          }
          pdf_manifest = {
              "name": "broker.pdf",
              "text": "retained pdf facts",
              "images": [],
              "method": "local_extraction",
          }
          projections = (
              (
                  file_handling._PdfAttachmentList(
                      [raw_pdf],
                      [native_attachment, {"contentType": "application/pdf"}],
                  ),
                  [(1, pdf_manifest)],
                  [pdf_manifest],
              ),
              (
                  file_handling._PdfAttachmentList([], [native_attachment]),
                  [],
                  [],
              ),
          )

          for pdf_projection, processed_pdfs, expected in projections:
              with self.subTest(expected_count=len(expected)):
                  with mock.patch.dict(
                      os.environ,
                      {"SITESIFT_NATIVE_IMAGE_INGESTION": "false"},
                      clear=False,
                  ), mock.patch.object(
                      file_handling,
                      "fetch_pdf_attachments",
                      return_value=pdf_projection,
                  ), mock.patch.object(
                      file_handling,
                      "_process_pdf_attachment_batch",
                      return_value=processed_pdfs,
                  ), mock.patch.object(
                      file_handling,
                      "validate_and_normalize_native_image_attachments",
                      side_effect=AssertionError(
                          "native validator called while release gate was false"
                      ),
                  ) as validator, mock.patch.object(
                      file_handling,
                      "build_native_image_manifest_entry",
                  ) as success_builder, mock.patch.object(
                      file_handling,
                      "build_native_image_failure_manifest_entry",
                  ) as failure_builder:
                      actual = file_handling.fetch_and_process_pdfs(
                          {"Authorization": "Bearer synthetic"},
                          "synthetic-message",
                          target_property_hint=(
                              "123 Sample Road, Example City, AZ 85001"
                          ),
                      )

                  self.assertEqual(expected, actual)
                  validator.assert_not_called()
                  success_builder.assert_not_called()
                  failure_builder.assert_not_called()
  ```

- [ ] **Step 2: Prove a disabled native-looking PDF MIME claim is ignored, not
  reclassified as a PDF.**

  Add this second method to `NativeImageReleaseGateTests`:

  ```python
      def test_disabled_gate_never_reclassifies_native_claim_as_pdf(self):
          attachment = _attachment(
              "123 Sample Road Example City AZ 85001 mismatch.jpg",
              "application/pdf",
              b"%PDF-1.7 not actually an image",
          )
          response = mock.MagicMock()
          response.status_code = 200
          response.json.return_value = {"value": [attachment]}

          with mock.patch.dict(
              os.environ,
              {"SITESIFT_NATIVE_IMAGE_INGESTION": "false"},
              clear=False,
          ), mock.patch.object(
              file_handling.requests,
              "get",
              return_value=response,
          ), mock.patch.object(
              file_handling,
              "process_pdf_for_ai",
          ) as pdf_processor:
              actual = file_handling.fetch_and_process_pdfs(
                  {"Authorization": "Bearer synthetic"},
                  "synthetic-mismatch-message",
                  target_property_hint=(
                      "123 Sample Road, Example City, AZ 85001"
                  ),
              )

          self.assertEqual([], actual)
          pdf_processor.assert_not_called()
  ```

- [ ] **Step 3: Explicitly enable the existing native integration class.**

  Add this method at the top of `NativeImageProcessingIntegrationTests`:

  ```python
      def setUp(self):
          gate = mock.patch.object(
              file_handling,
              "native_image_ingestion_enabled",
              return_value=True,
          )
          gate.start()
          self.addCleanup(gate.stop)
  ```

  This prevents the new production default from silently weakening the existing
  positive native tests.

- [ ] **Step 4: Run the release-gate class and prove RED.**

  Run:

  ```bash
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest -v \
      tests.test_image_attachment_ingestion.NativeImageReleaseGateTests
  ```

  Expected: FAIL includes
  `AssertionError: native validator called while release gate was false`; the
  mismatch case also demonstrates that the current active native path has not
  yet been darkened.

- [ ] **Step 5: Commit the RED boundary test.**

  ```bash
  git add tests/test_image_attachment_ingestion.py
  git commit -m "test: expose native processing behind a false gate"
  ```

### Task 5: Return before native validation while retaining PDFs

**Files:**

- Modify: `email_automation/file_handling.py`
- Test: `tests/test_image_attachment_ingestion.py`

- [ ] **Step 1: Import the predicate through the testable module seam.**

  Add beside the existing relative imports:

  ```python
  from .app_config import native_image_ingestion_enabled
  ```

- [ ] **Step 2: Add the early return after PDF processing and before native positions.**

  In `fetch_and_process_pdfs()`, keep construction of `positioned_entries`, then
  insert exactly:

  ```python
      if not native_image_ingestion_enabled():
          positioned_entries.sort(key=lambda item: (item[0], item[1]))
          return [
              entry
              for _position, _priority, entry in positioned_entries
          ]
  ```

  `native_positions`, native validation, success/failure manifest construction,
  and native ordering remain below this return unchanged.

- [ ] **Step 3: Run the full image module.**

  ```bash
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest -v tests.test_image_attachment_ingestion
  ```

  Expected: `OK`; disabled mixed/image-only cases pass, and every existing
  explicitly enabled native test remains green.

- [ ] **Step 4: Commit the minimal production boundary.**

  ```bash
  git add email_automation/file_handling.py \
    tests/test_image_attachment_ingestion.py
  git commit -m "feat: keep native image effects dark by default"
  ```

### Task 6: Pin the closed controller contract with a RED commit

**Files:**

- Modify: `tests/test_process_user_phase1_rollout_contract.py`
- Test: `tests/test_process_user_phase1_rollout_contract.py`

- [ ] **Step 1: Replace the existing stale controller packet assertion.**

  In `tests/test_process_user_phase1_rollout_contract.py`, replace the existing
  constants at lines 21–26 and replace—not parallel-add—the stale literal in
  `ValidatorTests.test_controller_pins_current_promoted_production_baseline`
  (currently line 305):

  Replace its rollback constants with:

  ```python
  OLD_REVISION = "process-user-stage-9491133f15d5"
  OLD_IMAGE = (
      "us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/"
      "process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968"
  )
  RELEASE_BRANCH = "feat/native-image-attachment-ingestion-20260816"
  ```

  In the existing `expected` dictionary, replace only its branch entry with:

  ```python
  "branch": RELEASE_BRANCH,
  ```

  Keep its existing `"old revision": OLD_REVISION`, `"old image": OLD_IMAGE`,
  rules/UI/hash, and auxiliary-tag entries unchanged.

  Do not leave `fix/scanned-pdf-production-candidate-20260816` anywhere in this
  test module. Task 7 proves its removal before controller GREEN.

  Build the revision environment in a local list and append the candidate-only
  gate:

  ```python
  environment = [
      {"name": "FIREBASE_BUCKET", "value": "bucket"},
      {
          "name": "OPENAI_API_KEY",
          "valueFrom": {
              "secretKeyRef": {
                  "name": "OPENAI_API_KEY",
                  "key": "latest",
              }
          },
      },
  ]
  if is_candidate:
      environment.append({
          "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
          "value": "false",
      })
  ```

  Use `"env": environment` in the returned revision.

  Rename the existing validator test so its name reflects the deliberate exact
  candidate delta:

  ```python
  def test_candidate_must_be_ready_and_exact_dark_config_delta(self):
  ```

- [ ] **Step 2: Add exact controller packet and malformed-gate tests.**

  Add to the validator test class:

  ```python
      def test_release_packet_is_bound_to_branch_and_live_rollback(self):
          self.assertEqual(
              "feat/native-image-attachment-ingestion-20260816",
              phase1_rollout.BRANCH,
          )
          self.assertEqual(OLD_REVISION, phase1_rollout.OLD_REVISION)
          self.assertEqual(OLD_IMAGE, phase1_rollout.OLD_IMAGE)

      def test_candidate_requires_one_exact_false_native_image_gate(self):
          baseline = revision(OLD_REVISION, OLD_IMAGE)
          valid = revision(CANDIDATE, CANDIDATE_IMAGE)
          phase1_rollout.validate_candidate(
              valid,
              baseline,
              CANDIDATE,
              CANDIDATE_IMAGE,
          )

          def gate_entry(value):
              return {
                  "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
                  "value": value,
              }

          invalid_candidates = []
          missing = revision(CANDIDATE, CANDIDATE_IMAGE)
          missing["spec"]["containers"][0]["env"] = [
              entry
              for entry in missing["spec"]["containers"][0]["env"]
              if entry.get("name") != "SITESIFT_NATIVE_IMAGE_INGESTION"
          ]
          invalid_candidates.append(missing)
          for bad_value in ("true", "False", " false "):
              changed = revision(CANDIDATE, CANDIDATE_IMAGE)
              next(
                  entry
                  for entry in changed["spec"]["containers"][0]["env"]
                  if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
              )["value"] = bad_value
              invalid_candidates.append(changed)
          duplicated = revision(CANDIDATE, CANDIDATE_IMAGE)
          duplicated["spec"]["containers"][0]["env"].append(
              gate_entry("false")
          )
          invalid_candidates.append(duplicated)

          secret_bound = revision(CANDIDATE, CANDIDATE_IMAGE)
          secret_gate = next(
              entry
              for entry in secret_bound["spec"]["containers"][0]["env"]
              if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
          )
          secret_gate.pop("value")
          secret_gate["valueFrom"] = {
              "secretKeyRef": {
                  "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
                  "key": "latest",
              }
          }
          invalid_candidates.append(secret_bound)

          extra_keyed = revision(CANDIDATE, CANDIDATE_IMAGE)
          next(
              entry
              for entry in extra_keyed["spec"]["containers"][0]["env"]
              if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
          )["unexpected"] = "rejected"
          invalid_candidates.append(extra_keyed)

          for candidate in invalid_candidates:
              with self.subTest(candidate=candidate):
                  with self.assertRaises(phase1_rollout.RolloutError):
                      phase1_rollout.validate_candidate(
                          candidate,
                          baseline,
                          CANDIDATE,
                          CANDIDATE_IMAGE,
                      )

          polluted_baseline = revision(OLD_REVISION, OLD_IMAGE)
          polluted_baseline["spec"]["containers"][0]["env"].append(
              gate_entry("false")
          )
          with self.assertRaises(phase1_rollout.RolloutError):
              phase1_rollout.validate_candidate(
                  valid,
                  polluted_baseline,
                  CANDIDATE,
                  CANDIDATE_IMAGE,
              )
  ```

- [ ] **Step 3: Add an injectable after-tag-removal drift seam to the fake.**

  Add `self.post_tag_removal_fault = None` and
  `self.reject_service_access = False` in `FakeOps.__init__()`. Extend the two
  affected fake methods exactly as follows; this changes test state only:

  ```python
      def verify_service_access(self, topology):
          self.events.append("service-access")
          if self.reject_service_access or topology.service_url != SERVICE_URL:
              raise phase1_rollout.RolloutError("service access rejected")

      def remove_cert_tag(self, tag):
          self.events.append("tag:remove")
          if self.fail_remove:
              raise phase1_rollout.RolloutError("remove failed")
          self.service = service()
          if self.post_tag_removal_fault == "candidate":
              gate = next(
                  entry
                  for entry in self.candidate_revision["spec"]["containers"][0]["env"]
                  if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
              )
              gate["value"] = "true"
          elif self.post_tag_removal_fault == "rollback":
              wrong = OLD_IMAGE.rsplit("sha256:", 1)[0] + "sha256:" + "d" * 64
              self.old_revision["spec"]["containers"][0]["image"] = wrong
              self.old_revision["status"]["imageDigest"] = wrong
          elif self.post_tag_removal_fault == "switches":
              self.fail_prerequisites = True
          elif self.post_tag_removal_fault == "topology":
              self.service = service(CANDIDATE, OLD_REVISION)
          elif self.post_tag_removal_fault == "iam":
              self.reject_service_access = True
          elif self.post_tag_removal_fault == "queue":
              self.queue["state"] = "RUNNING"
  ```

- [ ] **Step 4: Pin the exact locked, paused pre-promotion read sequence.**

  Replace the loose tag/remove/promote ordering assertion in the happy-path
  state-machine test with this exact slice assertion. It proves every
  authorization read occurs after temporary-tag removal and before promotion:

  ```python
          remove_index = ops.events.index("tag:remove")
          promote_index = ops.events.index("promote")
          self.assertEqual(
              [
                  "lock:assert",
                  "tag:remove",
                  "lock:assert",
                  "service",
                  "lock:assert",
                  "artifact",
                  f"revision:{OLD_REVISION}",
                  f"revision:{CANDIDATE}",
                  "prerequisites",
                  "service",
                  "service-access",
                  "queue",
                  "tasks",
                  "lock:assert",
                  "lock:assert",
                  "promote",
                  "lock:assert",
              ],
              ops.events[remove_index - 1:promote_index + 2],
          )
  ```

  Add the fail-closed matrix:

  ```python
      def test_every_post_tag_pre_promotion_revalidation_failure_stops_traffic(self):
          for fault in (
              "candidate",
              "rollback",
              "switches",
              "topology",
              "iam",
              "queue",
          ):
              with self.subTest(fault=fault):
                  ops = FakeOps()
                  ops.post_tag_removal_fault = fault
                  rollout, _ = self.make_rollout(ops)
                  with self.assertRaises(phase1_rollout.RolloutError):
                      rollout.apply()
                  self.assertIn("tag:remove", ops.events)
                  self.assertNotIn("promote", ops.events)
  ```

  Keep the existing task-appearance and lock-loss tests; together they prove
  that the empty-inventory and enclosing-lock assertions also fail closed.

- [ ] **Step 5: Run only the controller contract and prove RED.**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
    tests.test_process_user_phase1_rollout_contract
  ```

  Expected: failures identify the old branch/rollback constants, missing exact
  gate handling, and missing post-tag revalidation. No real `gcloud` call occurs;
  this module uses injected fakes.

- [ ] **Step 6: Commit only the controller RED contract.**

  ```bash
  git add tests/test_process_user_phase1_rollout_contract.py
  git commit -m "test: pin closed dark native promotion contract"
  ```

  Expected: one known RED component and no unrelated test or documentation
  change. Task 7 is next and must make this exact module GREEN before Task 8.

### Task 7: Rebind and close the Phase 1 controller

**Files:**

- Modify: `scripts/phase1_rollout.py`
- Test: `tests/test_process_user_phase1_rollout_contract.py`

- [ ] **Step 1: Replace the release identity constants.**

  Use exactly:

  ```python
  BRANCH = "feat/native-image-attachment-ingestion-20260816"
  OLD_REVISION = "process-user-stage-9491133f15d5"
  OLD_IMAGE = (
      IMAGE_REPOSITORY
      + "@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968"
  )
  NATIVE_IMAGE_GATE_NAME = "SITESIFT_NATIVE_IMAGE_INGESTION"
  NATIVE_IMAGE_GATE_VALUE = "false"
  ```

- [ ] **Step 2: Make canonical config comparison understand exactly one
  deliberate candidate delta.**

  Replace `_canonical_revision_spec` with:

  ```python
  def _canonical_revision_spec(
      value: Any,
      *,
      require_native_image_gate: bool,
  ) -> dict[str, Any]:
      result = copy.deepcopy(_object(value, "revision spec"))
      containers = result.get("containers")
      if not isinstance(containers, list) or len(containers) != 1:
          raise RolloutError("revision must have exactly one container")
      container = _object(containers[0], "revision container")
      containers[0] = container
      environment = container.get("env")
      if not isinstance(environment, list) or not all(
          isinstance(entry, dict) and isinstance(entry.get("name"), str)
          for entry in environment
      ):
          raise RolloutError("revision environment shape is invalid")
      gate_entries = [
          entry
          for entry in environment
          if entry.get("name") == NATIVE_IMAGE_GATE_NAME
      ]
      expected_gate_entries = (
          [{"name": NATIVE_IMAGE_GATE_NAME, "value": NATIVE_IMAGE_GATE_VALUE}]
          if require_native_image_gate
          else []
      )
      if gate_entries != expected_gate_entries:
          raise RolloutError("native image release gate is not exact")
      container["env"] = [
          entry
          for entry in environment
          if entry.get("name") != NATIVE_IMAGE_GATE_NAME
      ]
      container.pop("image", None)
      return result
  ```

  Replace the candidate comparison with:

  ```python
  if _canonical_revision_spec(
      spec,
      require_native_image_gate=True,
  ) != _canonical_revision_spec(
      baseline_spec,
      require_native_image_gate=False,
  ):
      raise RolloutError(
          "candidate config differs from baseline beyond image and dark gate"
      )
  ```

- [ ] **Step 3: Add the locked, paused, post-tag pre-promotion gate.**

  Add immediately after `_locked_mutation()`:

  ```python
      def _validate_locked_pre_promotion(self, lock: RolloutLock) -> None:
          self.ops.assert_lock(lock)
          image = self.ops.artifact_image()
          old = self.ops.get_revision(OLD_REVISION)
          validate_old_revision(old)
          candidate = self.ops.get_revision(self.candidate)
          validate_candidate(candidate, old, self.candidate, image)
          self.ops.verify_rules_ui_switches()
          topology = validate_topology(
              self.ops.get_service(),
              expected_positive=OLD_REVISION,
              expected_release=OLD_REVISION,
              expected_aux=AUX_TAGS,
          )
          self.ops.verify_service_access(topology)
          validate_queue(self.ops.get_queue(), "PAUSED")
          if not self._tasks_are_empty():
              raise RolloutError("task appeared immediately before promotion")
          self.ops.assert_lock(lock)
  ```

  In `apply()`, keep the fenced tag removal and its direct topology readback.
  Immediately after that readback, replace the loose task/switch/second-pause
  block with:

  ```python
              tag_attempted = False
              self._validate_locked_pre_promotion(lock)
              traffic_attempted = True
              self._locked_mutation(
                  lock, lambda: self.ops.promote(self.candidate, OLD_REVISION)
              )
  ```

  The queue was paused and drained before the temporary tag was added. The new
  helper re-proves `PAUSED` after every other authorization read, while its
  opening/closing lock assertions fence the exact candidate digest/dark gate,
  rollback pair, switches, unpromoted topology, IAM, queue, and task inventory.
  No controller-cached object authorizes promotion.

- [ ] **Step 4: Prove the stale branch is replaced and the controller is GREEN.**

  ```bash
  ! rg -n 'fix/scanned-pdf-production-candidate-20260816' \
    scripts/phase1_rollout.py \
    tests/test_process_user_phase1_rollout_contract.py
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
    tests.test_process_user_phase1_rollout_contract
  ```

  Expected: `OK`; happy-path promotion and every rollback/lock failure path
  remain green; the exact post-tag call slice matches; exact false passes; and
  missing, true, padded, duplicate, secret-bound/`valueFrom`, and extra-keyed
  gates fail. The `rg` command prints nothing and exits 0 through `!`.

- [ ] **Step 5: Commit the controller GREEN immediately after its RED.**

  ```bash
  git add scripts/phase1_rollout.py \
    tests/test_process_user_phase1_rollout_contract.py
  git commit -m "release: bind dark native promotion controller"
  ```

  Expected: the immediately preceding controller RED is fully GREEN. Do not
  begin staging tests while this module has any known failure.

### Task 8: Pin exact-false tagless staging with a RED commit

**Files:**

- Modify: `tests/test_process_user_tagless_staging_contract.py`
- Modify: `tests/test_process_user_production_deploy_contract.py`
- Test: the same two files.

- [ ] **Step 1: Pin the deploy argument in both contract constants.**

  Add this exact field to each module's `ENV_VARS` immediately before outbound
  mode:

  ```python
  "SITESIFT_NATIVE_IMAGE_INGESTION=false:"
  ```

  Add these assertions to
  `test_deploy_explicitly_arms_only_the_internal_release_lane`:

  ```python
  self.assertIn("SITESIFT_NATIVE_IMAGE_INGESTION=false", env_vars)
  self.assertNotIn("SITESIFT_NATIVE_IMAGE_INGESTION=true", env_vars)
  ```

- [ ] **Step 2: Make the tagless fake expose every exact-gate hostile case.**

  In `revision_document()`, add the gate only for the candidate before `env` is
  constructed:

  ```python
  if is_baseline and scenario == "baseline_native_gate":
      values["SITESIFT_NATIVE_IMAGE_INGESTION"] = "false"
  if not is_baseline:
      values["SITESIFT_NATIVE_IMAGE_INGESTION"] = "false"
      if scenario == "candidate_missing_native_gate":
          values.pop("SITESIFT_NATIVE_IMAGE_INGESTION")
      elif scenario == "candidate_true_native_gate":
          values["SITESIFT_NATIVE_IMAGE_INGESTION"] = "true"
      elif scenario == "candidate_padded_native_gate":
          values["SITESIFT_NATIVE_IMAGE_INGESTION"] = " false "
  ```

  Immediately after `env.extend(...)`, add the structural hostile cases:

  ```python
  if not is_baseline and scenario in {
      "candidate_secret_native_gate",
      "candidate_extra_key_native_gate",
  }:
      gate = next(
          entry
          for entry in env
          if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
      )
      if scenario == "candidate_secret_native_gate":
          gate.pop("value")
          gate["valueFrom"] = {
              "secretKeyRef": {
                  "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
                  "key": "latest",
              }
          }
      else:
          gate["unexpected"] = "rejected"
  ```

- [ ] **Step 3: Add the exact tagless rejection matrix.**

  Add beside the existing candidate-environment rejection tests:

  ```python
      def test_candidate_missing_native_gate_is_rejected(self):
          self._assert_revision_refused("candidate_missing_native_gate")

      def test_candidate_true_native_gate_is_rejected(self):
          self._assert_revision_refused("candidate_true_native_gate")

      def test_candidate_padded_native_gate_is_rejected(self):
          self._assert_revision_refused("candidate_padded_native_gate")

      def test_candidate_secret_bound_native_gate_is_rejected(self):
          self._assert_revision_refused("candidate_secret_native_gate")

      def test_candidate_extra_keyed_native_gate_is_rejected(self):
          self._assert_revision_refused("candidate_extra_key_native_gate")

      def test_baseline_native_gate_is_rejected(self):
          self._assert_revision_refused("baseline_native_gate")
  ```

- [ ] **Step 4: Run only the staging/deploy components and prove RED.**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
    tests.test_process_user_tagless_staging_contract \
    tests.test_process_user_production_deploy_contract
  ```

  Expected: failures identify the missing deploy argument/readback and exact
  candidate-delta behavior. Fake `gcloud` commands only; no cloud call occurs.

- [ ] **Step 5: Commit only this component's RED tests.**

  ```bash
  git add \
    tests/test_process_user_tagless_staging_contract.py \
    tests/test_process_user_production_deploy_contract.py
  git commit -m "test: pin exact dark tagless staging contract"
  ```

  Expected: one known RED staging/deploy component. Task 9 immediately makes
  these exact modules GREEN before documentation tests begin.

### Task 9: Stage an exact false gate and prove it by readback

**Files:**

- Modify: `scripts/deploy_process_user.sh`
- Test: `tests/test_process_user_tagless_staging_contract.py`
- Test: `tests/test_process_user_production_deploy_contract.py`

- [ ] **Step 1: Add exact false to the deploy environment string.**

  Keep every existing field and add this field before outbound mode:

  ```bash
  SITESIFT_NATIVE_IMAGE_INGESTION=false
  ```

  The resulting assignment remains one `^:^`-delimited
  `--update-env-vars` value; do not switch to `--set-env-vars`.

- [ ] **Step 2: Require the false value in staged-revision readback.**

  Add to `expected_values` in the inline candidate validator:

  ```python
  "SITESIFT_NATIVE_IMAGE_INGESTION": "false",
  ```

- [ ] **Step 3: Canonicalize only the exact candidate gate before baseline
  comparison.**

  Replace the inline `canonical_spec` with:

  ```python
  def canonical_spec(value, *, require_native_image_gate):
      value = json.loads(json.dumps(value))
      containers = value.get("containers")
      if not isinstance(containers, list) or len(containers) != 1:
          refuse("revision must contain exactly one container")
      container = containers[0]
      environment = container.get("env")
      if not isinstance(environment, list) or not all(
          isinstance(entry, dict) and isinstance(entry.get("name"), str)
          for entry in environment
      ):
          refuse("revision environment shape is invalid")
      gate_entries = [
          entry
          for entry in environment
          if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
      ]
      expected_gate_entries = (
          [{
              "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
              "value": "false",
          }]
          if require_native_image_gate
          else []
      )
      if gate_entries != expected_gate_entries:
          refuse("native image release gate is not exact")
      container["env"] = [
          entry
          for entry in environment
          if entry.get("name") != "SITESIFT_NATIVE_IMAGE_INGESTION"
      ]
      container.pop("image", None)
      return value
  ```

  Compare with:

  ```python
  if canonical_spec(
      spec,
      require_native_image_gate=True,
  ) != canonical_spec(
      baseline_spec,
      require_native_image_gate=False,
  ):
      refuse(
          "candidate config differs from baseline beyond immutable image "
          "and the exact dark native gate"
      )
  ```

- [ ] **Step 4: Run both staging/deploy contract modules to GREEN.**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
    tests.test_process_user_tagless_staging_contract \
    tests.test_process_user_production_deploy_contract
  ```

  Expected: `OK`; deploy ordering, immutable digest, no-traffic/no-tag behavior,
  exact false gate, and every malformed gate—including secret-bound/`valueFrom`
  and extra-keyed entries—are green. No documentation RED exists yet.

- [ ] **Step 5: Commit the staged dark-state contract.**

  ```bash
  git add scripts/deploy_process_user.sh
  git commit -m "release: stage native ingestion disabled"
  ```

  Expected: the immediately preceding staging/deploy RED is fully GREEN. Do not
  begin README contract changes while either module has a known failure.

### Task 10: Pin release documentation with a RED commit

**Files:**

- Modify: `tests/test_process_user_production_deploy_contract.py`
- Test: `tests/test_process_user_production_deploy_contract.py`

- [ ] **Step 1: Replace the rollback test constants with the live pair.**

  Use exactly:

  ```python
  ROLLBACK_REVISION = "process-user-stage-9491133f15d5"
  ROLLBACK_DIGEST = (
      "sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968"
  )
  ```

- [ ] **Step 2: Stop rewriting placeholders in the test harness.**

  Change `_run()` to remove `replace_rollback_placeholders`, retain the existing
  fake setup, and execute the extracted block directly:

  ```python
      def _run(
          self,
          scenario: str = "ok",
          account: str | None = ACCOUNT,
      ):
          # Keep the existing environment and fake-state setup.
          runbook = self._extract_runbook()
          return subprocess.run(
              ["bash", "-c", runbook],
              cwd=REPO_ROOT,
              env=env,
              text=True,
              capture_output=True,
              check=False,
          )
  ```

  Delete `test_unedited_rollback_placeholders_fail_before_gcloud`; it describes
  the intentionally retired placeholder contract.

- [ ] **Step 3: Add exact pair and evidence-boundary assertions.**

  ```python
      def test_runbook_pins_exact_live_rollback_pair(self):
          runbook = self._extract_runbook()
          self.assertIn(f'ROLLBACK_REVISION="{ROLLBACK_REVISION}"', runbook)
          self.assertIn(
              f'EXPECTED_ROLLBACK_IMAGE="{ROLLBACK_IMAGE}"',
              runbook,
          )

      def test_readme_pins_dark_native_evidence_boundary(self):
          readme = DEPLOY_README.read_text(encoding="utf-8")
          self.assertIn("SITESIFT_NATIVE_IMAGE_INGESTION=false", readme)
          self.assertIn("Dormant and unproved live", readme)
          self.assertIn("provider-canary=not-run", readme)
  ```

- [ ] **Step 4: Prove only the README-bound component is RED.**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
    tests.test_process_user_production_deploy_contract
  ```

  Expected: failures are limited to the still-placeholder rollback block and
  missing evidence language; the already GREEN deploy-script assertions stay
  green.

- [ ] **Step 5: Commit only the README RED contract.**

  ```bash
  git add tests/test_process_user_production_deploy_contract.py
  git commit -m "test: pin current dark release documentation"
  ```

### Task 11: Document the current release and pin rollback proof

**Files:**

- Modify: `deploy/README.md`
- Test: `tests/test_process_user_production_deploy_contract.py`

- [ ] **Step 1: Add the current release packet under the tagless staging
  heading.**

  Insert:

  ```markdown
  Current 2026-08-16 release packet:

  - source branch: `feat/native-image-attachment-ingestion-20260816`;
  - production native-image state:
    `SITESIFT_NATIVE_IMAGE_INGESTION=false`;
  - rollback revision: `process-user-stage-9491133f15d5`;
  - rollback image:
    `us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968`.

  The final deployment SHA and candidate digest are resolved from the clean,
  reviewed implementation HEAD. Native image code is present but dormant; no
  native provider/mailbox/image/model/Drive/Sheets effect is claimed live, and
  the release record says `provider-canary=not-run`.
  ```

- [ ] **Step 2: Document the service feature variable.**

  Add this row to the environment documentation, clearly scoped to the live
  `process-user` service rather than the historical job scaffold:

  ```markdown
  | `SITESIFT_NATIVE_IMAGE_INGESTION` | `process-user` service env | Fail-closed feature gate. Only exact lowercase `true` enables native JPG/PNG effects. The 2026-08-16 production release pins exact lowercase `false`; an unset or malformed value is also disabled but is not an acceptable release readback. |
  ```

- [ ] **Step 3: Update the staging comparison language.**

  Replace the claim that candidate and baseline config are identical beyond
  image with:

  ```markdown
  The candidate matches the baseline revision configuration apart from its
  immutable image and exactly one plain candidate-only environment entry,
  `SITESIFT_NATIVE_IMAGE_INGESTION=false`. Missing, duplicate, secret-bound,
  extra-keyed, non-lowercase, padded, or enabled values fail closed.
  ```

- [ ] **Step 4: Pin the rollback-proof block.**

  Set its variables to:

  ```bash
  ROLLBACK_REVISION="process-user-stage-9491133f15d5"
  EXPECTED_ROLLBACK_IMAGE="us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968"
  ```

  Remove the obsolete unbound-variable guard. Keep the exact digest validation,
  release-image validation, traffic readbacks, and guaranteed Release A
  restoration trap unchanged. State immediately above the block that it is a
  separately authorized maintenance proof, not an automatic post-release step
  and not a provider canary.

- [ ] **Step 5: Add the evidence taxonomy.**

  Append a concise subsection with these exact labels:

  ```markdown
  ### Release evidence labels

  - **Proved offline:** deterministic tests and static checks.
  - **Proved live by control plane:** revision, digest, exact false gate,
    readiness, traffic, IAM, queue, switches, and authenticated health.
  - **Observed in routine live operation:** sanitized facts backed by production
    metrics or an earlier release receipt.
  - **Dormant and unproved live:** native image ingestion and all downstream
    native effects.
  - **Needs more examination:** any behavior without deterministic proof or
    suitable routine-live evidence.

  A health check is not a provider, mailbox, PDF, image, model, Drive, Sheets,
  or reply canary.
  ```

- [ ] **Step 6: Make the README contract GREEN and commit immediately.**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
    tests.test_process_user_production_deploy_contract
  git diff --check
  git add deploy/README.md
  git commit -m "docs: pin dark native release and rollback proof"
  ```

  Expected: `OK`, whitespace check exits 0, and the commit contains only the
  README. No known RED is carried into the offline verification task.

### Task 12: Run the full offline gate

**Files:** verification only across all nine implementation paths.

- [ ] **Step 1: Run native, PDF, predecessor, and retryability coverage.**

  ```bash
  set -euo pipefail
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest -v \
      tests.test_image_attachment_ingestion \
      tests.test_scanned_pdf_extraction \
      tests.test_mixed_pdf_asset_quarantine \
      tests.test_broker_language_broker_attachment_or_link_only \
      tests.test_inbound_authority_m1 \
      tests.test_processing_retryability
  ```

  Expected: `OK`; zero external effects. The total is greater than the prior
  retained matrix because the new gate cases are included.

- [ ] **Step 2: Run all release-control contracts together.**

  ```bash
  set -euo pipefail
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
    tests.test_process_user_phase1_rollout_contract \
    tests.test_process_user_tagless_staging_contract \
    tests.test_process_user_production_deploy_contract
  ```

  Expected: `OK`; fake command logs prove no real cloud effect.

- [ ] **Step 3: Run broad broker-language, Jill, AI, and processing regressions.**

  ```bash
  set -euo pipefail
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest discover -v \
      -s tests -p 'test_broker_language_*.py'

  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest discover -v \
      -s tests -p 'test_jill_*.py'

  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest discover -v \
      -s tests -p 'test_ai_*.py'

  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest discover -v \
      -s tests -p 'test_processing_*.py'
  ```

  Expected: all four commands end `OK`. If a known legacy dead-emulator leak
  appears, do not broaden credentials or call a live backend; isolate the exact
  failing module, compare it with the reviewed baseline, and record it as an
  instrument limitation rather than a feature pass.

- [ ] **Step 4: Compile, parse, and check whitespace.**

  ```bash
  set -euo pipefail
  python3 -m py_compile \
    email_automation/app_config.py \
    email_automation/file_handling.py \
    scripts/phase1_rollout.py \
    tests/test_image_attachment_ingestion.py \
    tests/test_process_user_phase1_rollout_contract.py \
    tests/test_process_user_tagless_staging_contract.py \
    tests/test_process_user_production_deploy_contract.py
  bash -n scripts/deploy_process_user.sh
  git diff --check
  ```

  Expected: every command exits 0.

- [ ] **Step 5: Run a second Python compiler when available.**

  ```bash
  set -euo pipefail
  for python_bin in /usr/bin/python3 /opt/homebrew/bin/python3; do
    if [[ -x "$python_bin" ]]; then
      "$python_bin" -m py_compile \
        email_automation/app_config.py \
        email_automation/file_handling.py \
        scripts/phase1_rollout.py
    fi
  done
  ```

  Expected: every available interpreter exits 0.

- [ ] **Step 6: Prove the implementation delta is exactly nine paths.**

  Run in a fresh shell; the block resolves its own planning boundary:

  ```bash
  set -euo pipefail
  PLANNING_COMMIT="$(git log -1 --format=%H -- \
    docs/superpowers/plans/2026-08-16-sitesift-dark-native-production-release.md)"
  PLANNING_COMMIT="$PLANNING_COMMIT" python3 - <<'PY'
  import os
  import subprocess

  expected = {
      "deploy/README.md",
      "email_automation/app_config.py",
      "email_automation/file_handling.py",
      "scripts/deploy_process_user.sh",
      "scripts/phase1_rollout.py",
      "tests/test_image_attachment_ingestion.py",
      "tests/test_process_user_phase1_rollout_contract.py",
      "tests/test_process_user_production_deploy_contract.py",
      "tests/test_process_user_tagless_staging_contract.py",
  }
  actual = set(subprocess.run(
      [
          "git",
          "diff",
          "--name-only",
          f"{os.environ['PLANNING_COMMIT']}..HEAD",
      ],
      check=True,
      text=True,
      stdout=subprocess.PIPE,
  ).stdout.splitlines())
  if actual != expected:
      raise SystemExit(
          f"implementation scope mismatch: missing={sorted(expected - actual)!r} "
          f"extra={sorted(actual - expected)!r}"
      )
  print("implementation scope: exact nine paths")
  PY
  git status --short
  ```

  Expected: `implementation scope: exact nine paths` and a clean worktree.

### Task 13: Obtain two independent reviews and rerun fresh verification

**Files:** review only; fixes remain limited to the exact nine paths.

- [ ] **Step 1: Dispatch a behavior/privacy reviewer.**

  Ask a fresh reviewer to verify:

  - only exact lowercase `true` enables native behavior;
  - false preserves PDF order/content and reaches no native validation, model,
    Drive, Sheets, or warning/reply manifest boundary;
  - native-looking PDF MIME mismatches do not become PDFs;
  - existing enabled native tests remain meaningful; and
  - logs/tests contain no sensitive attachment, property, mailbox, token, or
    prompt data.

  Expected verdict: zero P0/P1/P2 findings. Any finding is fixed with a new RED
  test and a new non-amended commit, then reviewed again.

- [ ] **Step 2: Dispatch a release/effect reviewer.**

  Ask a different fresh reviewer to verify:

  - exact branch and rollback pair;
  - candidate-only plain false gate as the sole config delta, with explicit
    secret-bound/`valueFrom` and extra-keyed rejection;
  - immutable untagged 0% staging;
  - exact after-tag-removal, under-lock, paused-queue revalidation order and a
    no-promotion failure test for each authorization read;
  - closed lock/queue/tag/promotion/rollback ordering;
  - independent pre-promotion config/rollback-digest proof and direct
    post-promotion switches/IAM/authenticated-health rereads;
  - self-contained shell blocks with no inherited release variable;
  - no POST/provider/mailbox canary path;
  - exact rollback-proof restoration semantics; and
  - honest post-live evidence labels.

  Expected verdict: zero P0/P1/P2 findings. Fix and re-review every finding.

- [ ] **Step 3: Rerun Task 12 from the reviewed exact HEAD.**

  Expected: all commands pass fresh; do not reuse earlier output.

### Task 14: Push the exact candidate and prove three-way parity

**Files:** no new edits.

- [ ] **Step 1: Record the final candidate identity.**

  ```bash
  set -euo pipefail
  BRANCH="feat/native-image-attachment-ingestion-20260816"
  HEAD_SHA="$(git rev-parse HEAD)"
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  test "$(git branch --show-current)" = "$BRANCH"
  test -z "$(git status --porcelain=v1)"
  printf 'candidate_sha=%s\ncandidate_revision=process-user-stage-%s\n' \
    "$HEAD_SHA" "$SHORT_SHA"
  ```

  Expected: clean exact branch and deterministic candidate revision.

- [ ] **Step 2: Push without force and without creating a PR.**

  ```bash
  set -euo pipefail
  BRANCH="feat/native-image-attachment-ingestion-20260816"
  git push origin "HEAD:refs/heads/${BRANCH}"
  ```

  Expected: ordinary successful push.

- [ ] **Step 3: Prove local, upstream, and remote equality.**

  ```bash
  set -euo pipefail
  BRANCH="feat/native-image-attachment-ingestion-20260816"
  LOCAL_SHA="$(git rev-parse HEAD)"
  UPSTREAM_SHA="$(git rev-parse '@{upstream}')"
  REMOTE_SHA="$(git ls-remote origin "refs/heads/${BRANCH}" | awk '{print $1}')"
  test "$LOCAL_SHA" = "$UPSTREAM_SHA"
  test "$LOCAL_SHA" = "$REMOTE_SHA"
  git status --short --branch
  ```

  Expected: all equality checks exit 0 and status is clean/ahead-by-zero.

### Task 15: Refresh gcloud credentials and run mutation-free release gates

**Files:** no edits.

- [ ] **Step 1: Establish the exact approved identity environment.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  ```

  Expected: exit 0 and no credential value printed.

- [ ] **Step 2: Refresh/read the access credential without exposing it.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  gcloud auth print-access-token \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" >/dev/null
  ```

  Expected: exit 0 and no stdout. If it fails, stop; the operator completes
  interactive reauthentication outside this release transcript, then restarts
  at Task 15 Step 1.

- [ ] **Step 3: Run both dry-runs.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  scripts/deploy_process_user.sh --dry-run
  GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT" \
    scripts/rollout_process_user_phase1.sh --dry-run
  ```

  Expected: deploy reports immutable build/deploy intent, untagged 0% staging,
  and zero gcloud commands; rollout reports unchanged RUNNING queue/traffic/tags.

- [ ] **Step 4: Re-prove source parity immediately before the first build.**

  Run in a fresh shell:

  ```bash
  set -euo pipefail
  BRANCH="feat/native-image-attachment-ingestion-20260816"
  LOCAL_SHA="$(git rev-parse HEAD)"
  UPSTREAM_SHA="$(git rev-parse '@{upstream}')"
  REMOTE_SHA="$(git ls-remote origin "refs/heads/${BRANCH}" | awk '{print $1}')"
  test "$(git branch --show-current)" = "$BRANCH"
  test -z "$(git status --porcelain=v1)"
  test "$LOCAL_SHA" = "$UPSTREAM_SHA"
  test "$LOCAL_SHA" = "$REMOTE_SHA"
  ```

  Expected: exact equality and clean status.

### Task 16: Stage the immutable candidate at 0% and stop for independent readback

**Files:** cloud state only through the reviewed tagless staging script.

- [ ] **Step 1: Execute tagless staging.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  scripts/deploy_process_user.sh --apply
  ```

  Expected: prerequisite verification succeeds; one image is built and resolved
  to a canonical digest; one deterministic candidate revision is Ready,
  untagged, and at 0%; the pinned rollback revision remains sole 100% with
  `release-a`; queue and traffic mutation commands are absent.

- [ ] **Step 2: Resolve the immutable candidate identity.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  HEAD_SHA="$(git rev-parse HEAD)"
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  CANDIDATE_REVISION="process-user-stage-${SHORT_SHA}"
  CANDIDATE_DIGEST="$(
    gcloud artifacts docker images describe \
      "us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user:${SHORT_SHA}" \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT" \
      '--format=value(image_summary.digest)'
  )"
  [[ "$CANDIDATE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
  CANDIDATE_IMAGE="us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user@${CANDIDATE_DIGEST}"
  printf 'candidate_sha=%s\ncandidate_revision=%s\ncandidate_image=%s\n' \
    "$HEAD_SHA" "$CANDIDATE_REVISION" "$CANDIDATE_IMAGE"
  ```

  Expected: exact SHA, deterministic revision, and canonical repository digest.

- [ ] **Step 3: Independently prove the candidate image, readiness, and false
  gate without printing the rest of its environment.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  CANDIDATE_REVISION="process-user-stage-${SHORT_SHA}"
  CANDIDATE_DIGEST="$(
    gcloud artifacts docker images describe \
      "us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user:${SHORT_SHA}" \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT" \
      '--format=value(image_summary.digest)'
  )"
  [[ "$CANDIDATE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
  CANDIDATE_IMAGE="us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user@${CANDIDATE_DIGEST}"
  gcloud run revisions describe "$CANDIDATE_REVISION" \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --region us-central1 \
    --format=json | \
    EXPECTED_REVISION="$CANDIDATE_REVISION" \
    EXPECTED_IMAGE="$CANDIDATE_IMAGE" \
    python3 -c '
  import json, os, sys
  revision = json.load(sys.stdin)
  assert revision.get("metadata", {}).get("name") == os.environ["EXPECTED_REVISION"]
  spec = revision.get("spec", {})
  containers = spec.get("containers", [])
  assert len(containers) == 1
  assert containers[0].get("image") == os.environ["EXPECTED_IMAGE"]
  gates = [
      entry for entry in containers[0].get("env", [])
      if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
  ]
  assert gates == [{"name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "false"}]
  ready = [
      row for row in revision.get("status", {}).get("conditions", [])
      if row.get("type") == "Ready"
  ]
  assert len(ready) == 1 and str(ready[0].get("status")).lower() == "true"
  print("candidate revision: image, readiness, and dark gate exact")
  '
  ```

  Expected: the single sanitized success line.

- [ ] **Step 4: Independently prove 0% and no candidate tag.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  CANDIDATE_REVISION="process-user-stage-${SHORT_SHA}"
  gcloud run services describe process-user \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --region us-central1 \
    --format=json | \
    CANDIDATE_REVISION="$CANDIDATE_REVISION" \
    ROLLBACK_REVISION="process-user-stage-9491133f15d5" \
    python3 -c '
  import json, os, sys
  service = json.load(sys.stdin)
  routes = service.get("status", {}).get("traffic", [])
  candidate = os.environ["CANDIDATE_REVISION"]
  rollback = os.environ["ROLLBACK_REVISION"]
  assert sum(row.get("percent", 0) for row in routes if row.get("revisionName") == candidate) == 0
  assert not any(row.get("revisionName") == candidate and row.get("tag") for row in routes)
  assert sum(row.get("percent", 0) for row in routes if row.get("revisionName") == rollback) == 100
  assert [row.get("revisionName") for row in routes if row.get("tag") == "release-a"] == [rollback]
  print("staging routing: candidate untagged at 0%; rollback sole 100%")
  '
  ```

  Expected: the single sanitized routing line.

- [ ] **Step 5: Independently prove rollback digest and sanitized config
  parity.**

  This is deliberately separate from both the stager and controller. It prints
  no environment value, secret reference, or revision document:

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  CANDIDATE_REVISION="process-user-stage-${SHORT_SHA}"
  CANDIDATE_DIGEST="$(
    gcloud artifacts docker images describe \
      "us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user:${SHORT_SHA}" \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT" \
      '--format=value(image_summary.digest)'
  )"
  [[ "$CANDIDATE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
  CANDIDATE_IMAGE="us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user@${CANDIDATE_DIGEST}"
  ROLLBACK_REVISION="process-user-stage-9491133f15d5"
  ROLLBACK_IMAGE="us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968"
  RELEASE_EVIDENCE_DIR="$(mktemp -d)"
  trap 'rm -rf -- "$RELEASE_EVIDENCE_DIR"' EXIT
  gcloud run revisions describe "$CANDIDATE_REVISION" \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --region us-central1 \
    --format=json >"$RELEASE_EVIDENCE_DIR/candidate.json"
  gcloud run revisions describe "$ROLLBACK_REVISION" \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --region us-central1 \
    --format=json >"$RELEASE_EVIDENCE_DIR/rollback.json"
  EXPECTED_CANDIDATE="$CANDIDATE_REVISION" \
  EXPECTED_CANDIDATE_IMAGE="$CANDIDATE_IMAGE" \
  EXPECTED_ROLLBACK="$ROLLBACK_REVISION" \
  EXPECTED_ROLLBACK_IMAGE="$ROLLBACK_IMAGE" \
    python3 - "$RELEASE_EVIDENCE_DIR/candidate.json" \
      "$RELEASE_EVIDENCE_DIR/rollback.json" <<'PY'
  import copy
  import json
  import os
  import sys

  with open(sys.argv[1], encoding="utf-8") as stream:
      candidate = json.load(stream)
  with open(sys.argv[2], encoding="utf-8") as stream:
      rollback = json.load(stream)

  gate_name = "SITESIFT_NATIVE_IMAGE_INGESTION"

  def canonical_spec(revision, expected_gate):
      spec = copy.deepcopy(revision["spec"])
      containers = spec.get("containers")
      assert isinstance(containers, list) and len(containers) == 1
      container = containers[0]
      environment = container.get("env")
      assert isinstance(environment, list)
      assert all(
          isinstance(entry, dict) and isinstance(entry.get("name"), str)
          for entry in environment
      )
      gates = [entry for entry in environment if entry.get("name") == gate_name]
      assert gates == expected_gate
      container["env"] = [
          entry for entry in environment if entry.get("name") != gate_name
      ]
      container.pop("image", None)
      return spec

  def canonical_metadata(revision):
      metadata = revision["metadata"]
      annotations = dict(metadata.get("annotations", {}))
      labels = dict(metadata.get("labels", {}))
      annotations.pop("run.googleapis.com/operation-id", None)
      labels.pop("serving.knative.dev/configurationGeneration", None)
      labels.pop("serving.knative.dev/route", None)
      return {"annotations": annotations, "labels": labels}

  assert candidate["metadata"]["name"] == os.environ["EXPECTED_CANDIDATE"]
  assert rollback["metadata"]["name"] == os.environ["EXPECTED_ROLLBACK"]
  assert candidate["spec"]["containers"][0]["image"] == os.environ[
      "EXPECTED_CANDIDATE_IMAGE"
  ]
  assert candidate["status"]["imageDigest"] == os.environ[
      "EXPECTED_CANDIDATE_IMAGE"
  ]
  assert rollback["spec"]["containers"][0]["image"] == os.environ[
      "EXPECTED_ROLLBACK_IMAGE"
  ]
  assert rollback["status"]["imageDigest"] == os.environ[
      "EXPECTED_ROLLBACK_IMAGE"
  ]
  assert canonical_spec(
      candidate,
      [{"name": gate_name, "value": "false"}],
  ) == canonical_spec(rollback, [])
  assert canonical_metadata(candidate) == canonical_metadata(rollback)
  print("staging parity: exact rollback digest and sole dark-gate delta")
  PY
  ```

  Expected: the single sanitized parity line. Any missing, duplicate,
  `valueFrom`/secret-bound, extra-keyed, nonexact, or baseline gate fails; any
  rollback digest or other functional config drift also fails.

- [ ] **Step 6: Have a fresh release reviewer inspect Steps 1–5.**

  Expected verdict: stage is promotable with zero P0/P1/P2 findings. The reviewer
  must state that native behavior is dormant and not live-proven. Any ambiguity
  stops before promotion.

### Task 17: Promote only through the closed controller

**Files:** cloud state only through the reviewed promotion controller.

- [ ] **Step 1: Run the exact controller.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT" \
    scripts/rollout_process_user_phase1.sh --apply
  ```

  Expected final line includes the exact candidate revision,
  `queue=RUNNING`, `switches=false,false`, and `provider-canary=not-run`.
  Internally, the controller must prove the lock, pause/drain snapshots,
  temporary authenticated health tag and its removal, then—under the same lock
  while the queue remains paused—fresh candidate digest/dark-gate, rollback,
  switches, topology, IAM, queue, and empty-task reads before promotion. It then
  proves post-promotion health and empty tasks before queue resume.

- [ ] **Step 2: Apply the failure rule without improvisation.**

  If the command exits nonzero, preserve its sanitized error and inspect only
  the controller’s final queue/traffic/readback state. Expected safe outcomes
  are either exact rollback at 100% plus RUNNING queue, or an explicit
  `MANUAL_RECOVERY` state with the queue kept paused. Do not run Task 18 and do
  not issue a direct traffic command.

### Task 18: Read back production and classify the evidence

**Files:** no repository edits; report results in the task response.

- [ ] **Step 1: Prove final routing.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  CANDIDATE_REVISION="process-user-stage-${SHORT_SHA}"
  gcloud run services describe process-user \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --region us-central1 \
    --format=json | \
    CANDIDATE_REVISION="$CANDIDATE_REVISION" \
    python3 -c '
  import json, os, sys
  service = json.load(sys.stdin)
  routes = service.get("status", {}).get("traffic", [])
  candidate = os.environ["CANDIDATE_REVISION"]
  positive = {
      row.get("revisionName"): row.get("percent")
      for row in routes if row.get("percent", 0) > 0
  }
  assert positive == {candidate: 100}
  assert [row.get("revisionName") for row in routes if row.get("tag") == "release-a"] == [candidate]
  print("production routing: candidate sole 100% and release-a exact")
  '
  ```

  Expected: the single sanitized success line.

- [ ] **Step 2: Directly re-read the candidate dark gate and immutable image.**

  Run this independently of the controller output:

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  CANDIDATE_REVISION="process-user-stage-${SHORT_SHA}"
  CANDIDATE_DIGEST="$(
    gcloud artifacts docker images describe \
      "us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user:${SHORT_SHA}" \
      --account "$PROCESS_USER_APPROVED_ACCOUNT" \
      --project "$PROCESS_USER_PROJECT" \
      '--format=value(image_summary.digest)'
  )"
  [[ "$CANDIDATE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
  CANDIDATE_IMAGE="us-central1-docker.pkg.dev/${PROCESS_USER_PROJECT}/cloud-run-source-deploy/process-user@${CANDIDATE_DIGEST}"
  gcloud run revisions describe "$CANDIDATE_REVISION" \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --region us-central1 \
    --format=json | \
    EXPECTED_REVISION="$CANDIDATE_REVISION" \
    EXPECTED_IMAGE="$CANDIDATE_IMAGE" \
    python3 -c '
  import json, os, sys
  revision = json.load(sys.stdin)
  assert revision.get("metadata", {}).get("name") == os.environ["EXPECTED_REVISION"]
  containers = revision.get("spec", {}).get("containers", [])
  assert len(containers) == 1
  assert containers[0].get("image") == os.environ["EXPECTED_IMAGE"]
  assert revision.get("status", {}).get("imageDigest") == os.environ["EXPECTED_IMAGE"]
  gates = [
      entry for entry in containers[0].get("env", [])
      if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
  ]
  assert gates == [{"name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "false"}]
  ready = [
      row for row in revision.get("status", {}).get("conditions", [])
      if row.get("type") == "Ready"
  ]
  assert len(ready) == 1 and str(ready[0].get("status")).lower() == "true"
  print("production candidate: image, readiness, and dark gate exact")
  '
  ```

  Expected: the single sanitized success line.

- [ ] **Step 3: Re-prove the rollback image without printing configuration.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  gcloud run revisions describe process-user-stage-9491133f15d5 \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --region us-central1 \
    '--format=value(spec.containers[0].image,status.imageDigest)'
  ```

  Expected: both fields equal
  `us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968`.

- [ ] **Step 4: Prove queue state and empty task inventory.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  gcloud tasks queues describe graph-process-user \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --location us-central1 \
    '--format=value(state)'
  gcloud tasks list \
    --queue graph-process-user \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --location us-central1 \
    --limit 1 \
    '--format=value(name)'
  ```

  Expected: first command prints `RUNNING`; second prints nothing.

- [ ] **Step 5: Directly re-read switches, IAM, and authenticated health.**

  This imports the reviewed read adapter but does not run the promotion state
  machine. It directly calls the underlying control-plane/HTTPS reads after
  promotion, so the evidence does not depend on the controller's success line:

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  python3 -B <<'PY'
  import importlib.util
  from pathlib import Path
  import sys

  root = Path.cwd()
  module_path = root / "scripts" / "phase1_rollout.py"
  spec = importlib.util.spec_from_file_location(
      "phase1_rollout_post_live_readback",
      module_path,
  )
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)

  head = module._current_head(root)
  ops = module.SubprocessOps(root, head)
  ops.preflight()
  ops.verify_rules_ui_switches()
  topology = module.validate_topology(
      ops.get_service(),
      expected_positive=ops.candidate,
      expected_release=ops.candidate,
      expected_aux=module.AUX_TAGS,
  )
  ops.verify_service_access(topology)
  module._validate_legacy_health(
      ops.legacy_health_get(topology.service_url, topology.service_url)
  )
  module._validate_legacy_health(
      ops.legacy_health_get(
          topology.tag_urls["release-a"],
          topology.service_url,
      )
  )
  print("direct post-live readback: switches false; IAM private; health exact")
  PY
  ```

  Expected: the single sanitized success line. This is authenticated
  `GET /health` only; it is not a worker POST, mailbox/provider canary, attachment
  test, or downstream effect proof.

- [ ] **Step 6: Read sanitized candidate error and request metadata.**

  ```bash
  set -euo pipefail
  source scripts/process_user_gcloud_preflight.sh
  export GCLOUD_ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN \
    CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
    CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT \
    CLOUDSDK_CORE_ACCOUNT \
    CLOUDSDK_CORE_PROJECT
  SHORT_SHA="$(git rev-parse --short=12 HEAD)"
  CANDIDATE_REVISION="process-user-stage-${SHORT_SHA}"
  gcloud logging read \
    'resource.type="cloud_run_revision" AND resource.labels.service_name="process-user" AND resource.labels.revision_name="'"$CANDIDATE_REVISION"'" AND severity>=ERROR' \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --freshness=30m \
    --limit=100 \
    '--format=table(timestamp,severity,resource.labels.revision_name,logName)'

  gcloud logging read \
    'resource.type="cloud_run_revision" AND resource.labels.service_name="process-user" AND resource.labels.revision_name="'"$CANDIDATE_REVISION"'" AND logName:"run.googleapis.com%2Frequests"' \
    --account "$PROCESS_USER_APPROVED_ACCOUNT" \
    --project "$PROCESS_USER_PROJECT" \
    --freshness=30m \
    --limit=100 \
    '--format=table(timestamp,httpRequest.status,httpRequest.latency,resource.labels.revision_name)'
  ```

  Expected: no candidate ERROR row attributable to release startup/health; any
  request rows contain only status/latency/revision metadata. Do not expand log
  payloads to investigate content in this release task.

- [ ] **Step 7: Deliver the standing report with five explicit sections.**

  The report must state:

  1. **Proved offline:** exact test counts, compile/shell/diff/scope results, and
     independent 0/0/0 reviews.
  2. **Proved live by control plane:** final SHA, branch parity, artifact digest,
     active revision, 100%/`release-a`, exact false gate, rollback pair,
     readiness and queue, plus the independent direct switches/IAM/authenticated-
     health reread and sanitized logs. Controller output is supporting evidence,
     not a substitute for these direct rereads.
  3. **Observed in routine live operation:** only previously recorded or newly
     observed sanitized production facts; if none occurred in the window, say
     “no routine behavioral event observed” rather than manufacturing a canary.
  4. **Dormant and unproved live:** native image parsing, binding, vision, Drive
     hosting, Property Image writes, and any native-triggered reply/provider
     effect. Also say `provider-canary=not-run`.
  5. **Needs more examination:** full-address coverage, split address/city rows,
     controlled native end-to-end evidence, varied long scanned PDFs,
     natural-English response quality, emulator-controlled broad tests, and a
     separately scheduled rollback drill.

  Do not claim the rollback-proof block ran unless a separate authorized
  maintenance drill actually ran. Do not claim any application behavior works
  live solely because its code is deployed or its offline tests pass.
