# Flyer Column Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route plural `Flyers` headers through the canonical asset pipeline so one flyer uses the existing cell, floorplans remain separate, and AI notes cannot displace attachment URLs.

**Architecture:** Add the same exact normalized alias at the dashboard configuration boundary and worker safety boundary. Preserve the existing attachment writer, which already provides non-destructive one-link-per-cell behavior and numbered same-family overflow columns.

**Tech Stack:** Node.js `node:test`, Firebase Functions, Python, `unittest`/`pytest`, Google Sheets API wrappers.

---

### Task 1: Prove dashboard classification for plural `Flyers`

**Files:**
- Modify: `/Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/codex-flyer-routing-20260715/functions/sheetColumnAnalysis.test.js`
- Modify: `/Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/codex-flyer-routing-20260715/functions/analyzeSheetColumnsFunction.test.js`
- Modify: `/Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/codex-flyer-routing-20260715/functions/sheetColumnAnalysis.js`

- [ ] **Step 1: Add a failing exact-alias assertion**

Add this assertion to `exact known outreach headers override incomplete AI mappings`:

```js
assert.equal(resolveCanonicalHeader("Flyers"), "flyer_link");
```

- [ ] **Step 2: Add a failing function-level regression**

Add a test that supplies conflicting AI output but requires deterministic classification:

```js
test("analyzeSheetColumns reserves plural Flyers for attachment links", async t => {
  const { analyzeSheetColumns, restore } = loadAnalyzeSheetColumns({
    mappings: {
      listing_comments: { actualColumn: "Flyers", confidence: 0.99 },
    },
    unmapped: [],
    warnings: [],
  });
  t.after(restore);

  const result = await analyzeSheetColumns({
    auth: { uid: "baylor-proof" },
    data: {
      excelBase64: workbookBase64(
        ["Property Address", "Email", "Flyers", "Floorplan"],
        ["101 Proof Way", "bp21harrison@gmail.com", "", ""]
      ),
    },
  });

  const flyers = result.columns.find(column => column.header === "Flyers");
  assert.deepEqual(
    {
      canonicalKey: flyers.canonicalKey,
      defaultMode: flyers.defaultMode,
      confidence: flyers.confidence,
    },
    { canonicalKey: "flyer_link", defaultMode: "track", confidence: 1 }
  );
});
```

- [ ] **Step 3: Run both tests and verify RED**

Run:

```bash
cd functions
node --test sheetColumnAnalysis.test.js analyzeSheetColumnsFunction.test.js
```

Expected: failures showing `Flyers` resolves to `null` or the conflicting AI field instead of `flyer_link`.

- [ ] **Step 4: Add the minimal dashboard alias**

Change the `flyer_link` alias list in `sheetColumnAnalysis.js` to include the exact normalized plural:

```js
flyer_link: [
  "flyer link",
  "flyer",
  "flyers",
  "brochure link",
  "marketing package link",
  "listing link",
],
```

- [ ] **Step 5: Run both tests and verify GREEN**

Run the Step 3 command. Expected: all tests pass with zero failures.

- [ ] **Step 6: Commit the dashboard change**

```bash
git add functions/sheetColumnAnalysis.js functions/sheetColumnAnalysis.test.js functions/analyzeSheetColumnsFunction.test.js
git commit -m "fix: classify plural flyer columns"
```

### Task 2: Protect legacy campaign configs in the worker

**Files:**
- Modify: `tests/test_broker_language_broker_attachment_or_link_only.py`
- Modify: `tests/test_asset_column_routing.py`
- Modify: `email_automation/column_config.py`

- [ ] **Step 1: Add a failing legacy-config classifier assertion**

Extend `test_asset_column_classifier_uses_default_and_custom_mappings` with:

```python
legacy_config = {
    "mappings": {},
    "customFields": {
        "Flyers": {"mode": "note", "description": "Extract value for Flyers"}
    },
}
self.assertTrue(cc.is_asset_column_name("Flyers", legacy_config))
```

- [ ] **Step 2: Add a failing AI-write guard regression**

Add a test beside `test_apply_sheet_rejects_custom_mapped_asset_column` that invokes the real `apply_proposal_to_sheet` with header `Flyers`, an empty flyer cell, the legacy config above, and this update:

```python
{"column": "Flyers", "value": "Attached flyer provided (broker-flyer.pdf)."}
```

Assert `result["applied"] == []`, the skipped pair contains `("Flyers", "handled-by-asset-pipeline")`, and Sheets `batchUpdate` is not called.

- [ ] **Step 3: Add a failing one-flyer routing regression**

Add this writer-level test to `test_asset_column_routing.py`:

```python
def test_single_flyer_uses_existing_plural_column_without_overflow(self):
    service = FakeSheetsService(["Property Address", "Flyers", "Floorplan"])

    updates = sheets.append_links_to_flyer_link_column(
        service,
        "sheet-1",
        list(service.headers),
        3,
        ["https://drive.google.com/file/d/flyer/view"],
    )

    self.assertEqual(
        {"Flyers": ["https://drive.google.com/file/d/flyer/view"]},
        updates,
    )
    self.assertEqual(
        ["Property Address", "Flyers", "Floorplan"],
        service.headers,
    )
    self.assertEqual(
        "https://drive.google.com/file/d/flyer/view",
        service.cells["FOR LEASE!B3"],
    )
```

- [ ] **Step 4: Run focused worker tests and verify RED**

Run:

```bash
python3 -m pytest -q \
  tests/test_broker_language_broker_attachment_or_link_only.py \
  tests/test_asset_column_routing.py
```

Expected: the plural classifier and AI-write guard fail because `Flyers` is absent from canonical aliases. The writer-only regression may already pass, confirming the downstream writer is not the defect.

- [ ] **Step 5: Add the minimal worker alias**

Change the `flyer_link.default_aliases` entry in `column_config.py` to:

```python
"default_aliases": [
    "flyer / link",
    "flyer/link",
    "flyer",
    "flyers",
    "link",
    "links",
    "brochure",
    "listing link",
],
```

- [ ] **Step 6: Run focused worker tests and verify GREEN**

Run the Step 4 command. Expected: all tests pass with zero failures.

- [ ] **Step 7: Commit the worker change**

```bash
git add email_automation/column_config.py \
  tests/test_broker_language_broker_attachment_or_link_only.py \
  tests/test_asset_column_routing.py
git commit -m "fix: reserve plural flyer columns for assets"
```

### Task 3: Verify the cross-repo release contract

**Files:**
- Verify only; no planned production-code changes.

- [ ] **Step 1: Run the complete dashboard functions tests**

```bash
cd /Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/codex-flyer-routing-20260715
node --test functions/*.test.js
```

Expected: zero failures.

- [ ] **Step 2: Run the dashboard production build**

```bash
cd /Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/codex-flyer-routing-20260715
npm run build
```

Expected: build exits zero.

- [ ] **Step 3: Run the worker asset and extraction safety suites**

```bash
cd /Users/baylorharrison/.config/superpowers/worktrees/EmailAutomation/codex-flyer-routing-20260715
python3 -m pytest -q \
  tests/test_asset_column_routing.py \
  tests/test_broker_language_broker_attachment_or_link_only.py \
  tests/test_surface_b_extraction.py \
  tests/test_full_campaign_e2e.py
```

Expected: zero failures.

- [ ] **Step 4: Review diffs against current origins**

```bash
git diff origin/main...HEAD --check
git diff origin/main...HEAD
```

Expected: only the design/plan, plural aliases, and focused regressions are present.

- [ ] **Step 5: Request code review before deployment**

Review for unintended alias collisions, legacy custom-field precedence, asset overwrite risk, and missing tests. Address actionable findings and rerun Steps 1-4.

### Code Review Follow-Up: Reserve numbered asset-family columns

The independent review identified that the attachment writer recognizes numbered
asset-family headers such as `Flyers 2`, while the AI-write guard originally
recognized only exact base aliases. The implementation must keep those definitions
aligned.

- [ ] Add regressions proving `Flyers 2`, `Floorplan 3`, and a numbered custom
  mapping such as `Offering Materials 2` are asset columns, while `Flyers 1` is not.
- [ ] Prove the real AI-write path skips text proposals targeting numbered flyer
  and floorplan columns as `handled-by-asset-pipeline`.
- [ ] In `is_asset_column_name`, compare both the normalized physical header and,
  only when the suffix is an integer of at least 2, its unsuffixed base against
  configured and canonical asset aliases.
- [ ] Rerun the focused red-green tests, the 56-test worker safety suite, the
  175-test dashboard Functions suite, and the production dashboard build.

### Task 4: Deploy and prove through the browser

**Files:**
- Evidence update: `/Users/baylorharrison/Documents/SiteSiftEvidence/2026-07-15-final-browser-campaign/checkpoint.md`
- Vault update through `$VAULT/.agent-state/bin` helpers only.

- [ ] **Step 1: Merge or deploy the reviewed dashboard and worker commits using the repositories' established release paths**

Record exact commit SHAs and deployment identifiers. Do not enable normal users or mutate Jill's campaign.

- [ ] **Step 2: Run a fresh Baylor-owned browser campaign**

Use a sheet with existing `Flyers` and `Floorplan` columns. Send one broker reply containing one flyer PDF and one floorplan PDF through the normal mailbox path.

- [ ] **Step 3: Verify the release gate**

Confirm the resulting row has one clickable flyer URL in `Flyers`, one clickable floorplan URL in `Floorplan`, no `Flyers 2`, no `Property Image`, and no AI attachment prose in either asset column.

- [ ] **Step 4: Verify operational cleanup**

Confirm the test conversation reaches its intended terminal state, campaign status is intentionally closed, queues/dead letters are clean, usage math records the run, and production access remains at the approved Jill-only posture.

- [ ] **Step 5: Update evidence and the Active Experiment**

Record the browser proof, sheet state, campaign identifiers, usage math, and remaining release blockers. Mark the flyer-routing gate complete only if every Step 3 and Step 4 check passes.
