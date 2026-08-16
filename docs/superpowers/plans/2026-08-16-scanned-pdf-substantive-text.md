# Scanned PDF Substantive-Text Fallback Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to
> execute this plan with strict RED/GREEN evidence, then separate specification
> and code-quality reviews.

**Goal:** Prevent generated PDF page labels from falsely satisfying the local
text-extraction threshold so long image-only PDFs retain a full-file fallback.

**Deliverable:** code.

**Architecture:** Preserve the existing extraction output and manifest schema.
Add one pure threshold helper in `file_handling.py` that removes only exact
generated page-marker lines. Use its length for the current local-success
decision; leave upload, image caps, AI request construction, and failure behavior
unchanged.

**Tech stack:** Python 3, `unittest`, PyMuPDF (`fitz`), existing pdfplumber/Pillow
extraction path, and `unittest.mock` only at the OpenAI upload/Responses network
boundaries.

**Safety:** Every command runs with local no-credentials test settings. No live
provider, mailbox, Graph, Firestore, Sheets, browser, deployment, or outbound
action is allowed.

---

## Task 1: Reproduce the seven-page marker-only false positive

**Files:**

- Create: `tests/test_scanned_pdf_extraction.py`

- [ ] Add a deterministic helper that creates a real seven-page image-only PDF.
  Draw each page into a pixmap/image before inserting it into the PDF so the
  resulting page has visible content but no extractable text.
- [ ] Assert `extract_pdf_text()` preserves page markers for pages 1 through 7,
  returns page images, and has no non-marker substantive text.
- [ ] Patch only `upload_pdf_user_data`, call real `process_pdf_for_ai()`, and
  assert one upload call, `method == "openai_upload+images"`, retained identical
  `file_id`/`id`, and the existing five-image manifest cap.
- [ ] Pass the real processed manifest to real `propose_sheet_updates()` with a
  complete synthetic column contract and conversation. Replace only
  `client.responses.create`; assert its content has exactly one `input_file`
  using the retained ID, three `input_image` previews, and one trailing
  `input_text` prompt.
- [ ] Add a real native-text PDF negative control above 100 characters and prove
  `upload_pdf_user_data` is not called, `file_id` stays absent, and method stays
  `local_extraction`.
- [ ] Run the new test against the unchanged production code and capture an
  expected failure specifically showing the seven-page scan took
  `local_extraction+images` and skipped upload:

  ```bash
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest -v tests.test_scanned_pdf_extraction
  ```

## Task 2: Make the threshold count substantive text only

**Files:**

- Modify: `email_automation/file_handling.py`
- Test: `tests/test_scanned_pdf_extraction.py`

- [ ] Add one module-level compiled regex matching only full generated marker
  lines and one pure helper returning the trimmed threshold projection.
- [ ] Replace only the local-success length operand in `process_pdf_for_ai()`;
  preserve the returned marked text, manifest keys, method values, image caps,
  upload exception path, and logging behavior.
- [ ] Re-run the focused test and require all cases green.
- [ ] Inspect the diff for broad marker removal, manifest drift, or added network
  seams. Commit the test and minimal production change as one TDD unit.

## Task 3: Verify adjacent PDF and extraction behavior

**Files:** verification only.

- [ ] Run the focused and mixed-PDF controls:

  ```bash
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest -v \
      tests.test_scanned_pdf_extraction \
      tests.test_mixed_pdf_asset_quarantine
  ```

- [ ] Run the neighboring attachment/AI request controls:

  ```bash
  env -u GOOGLE_APPLICATION_CREDENTIALS \
    PYTHONDONTWRITEBYTECODE=1 E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    python3 -B -m unittest -v \
      tests.test_broker_language_broker_attachment_or_link_only \
      tests.test_property_image_resolver \
      tests.test_pdf_link_changelog
  ```

- [ ] Run syntax and whitespace checks:

  ```bash
  python3 -m py_compile \
    email_automation/file_handling.py \
    tests/test_scanned_pdf_extraction.py
  git diff --check
  git status --short
  ```

- [ ] Dispatch a fresh specification reviewer. Fix every behavioral gap with a
  RED first and repeat review until compliant.
- [ ] After specification review passes, dispatch a fresh code-quality reviewer.
  Fix and re-review every P0/P1 issue.
- [ ] Run the complete verification matrix fresh on the reviewed commit and
  prove the branch has only the approved source, test, design, and plan paths.
