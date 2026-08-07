# Explicit OpEx Precedence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an earlier `$X NNN` rent-basis phrase from overriding a later explicit OpEx/CAM dollar figure in a broker reply.

**Architecture:** Add one sentence-bounded, keyword-first OpEx candidate matcher. Preserve the existing combined base-plus-OpEx matcher as the highest-specificity path, evaluate the explicit candidate second, and retain the ambiguous/general matcher as the final fallback. Preserve monthly normalization, hypothetical-language, NNN rent-line, and proposal-reconciliation behavior; the deterministic value will continue to replace an unsupported model value through the existing `_fill` path.

**Tech Stack:** Python 3, `re`, `unittest`, pytest, existing `email_automation.ai_processing` extraction pipeline.

---

### Task 1: Reproduce the production extraction and write-path failures

**Files:**
- Modify: `tests/test_jill_live_campaign_regressions.py`
- Test: `tests/test_jill_live_campaign_regressions.py`

- [ ] **Step 1: Add the exact extractor regression**

Add this method to `JillLiveCampaignRegressionTests`:

```python
    def test_explicit_cam_figure_wins_over_earlier_nnn_rent_basis(self):
        text = (
            "For Space Center, we can offer 18,750 SF at $14.10 NNN. "
            "CAM, taxes, and insurance are running roughly $3.90 per square foot. "
            "The suite has one drive-in and two dock-high doors, 26 feet clear, "
            "277/480V three-phase 600-amp service, and was completed in 2008."
        )

        self.assertEqual("14.10", ai_processing._extract_rent_sf_yr_from_text(text))
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))
```

- [ ] **Step 2: Add the proposal-reconciliation regression**

Add this method to the same class:

```python
    def test_explicit_cam_replaces_conflicting_model_opex_before_sheet_write(self):
        text = (
            "For Space Center, we can offer 18,750 SF at $14.10 NNN. "
            "CAM, taxes, and insurance are running roughly $3.90 per square foot."
        )
        proposal = {
            "updates": [
                {"column": "Rent/SF/Yr", "value": "14.10"},
                {"column": "Ops Ex / SF", "value": "14.10"},
            ],
            "events": [],
        }
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {
            "mappings": {
                "rent_sf_yr": "Rent/SF/Yr",
                "ops_ex_sf": "Ops Ex / SF",
            }
        }

        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal,
            ["4800 Space Center Blvd", "", ""],
            header,
            config,
            _conversation(text),
        )

        self.assertEqual(
            "3.90",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_pending_cam_clause_does_not_capture_later_asking_rent(self):
        text = "CAM is still pending; the asking rent is $14.10 per square foot NNN."

        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))
```

- [ ] **Step 3: Run the two tests and verify RED**

Run:

```bash
E2E_TEST_MODE=true python3 -m pytest \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_explicit_cam_figure_wins_over_earlier_nnn_rent_basis \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_explicit_cam_replaces_conflicting_model_opex_before_sheet_write \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_pending_cam_clause_does_not_capture_later_asking_rent \
  -q
```

Expected: the first two tests fail because the actual OpEx value is `14.10`; the negative guard may already pass.

- [ ] **Step 4: Commit the RED tests**

```bash
git add tests/test_jill_live_campaign_regressions.py
git commit -m "test: reproduce explicit opex precedence regression"
```

### Task 2: Implement explicit keyword-first OpEx precedence

**Files:**
- Modify: `email_automation/ai_processing.py:1535-1880`
- Test: `tests/test_jill_live_campaign_regressions.py`

- [ ] **Step 1: Add the sentence-bounded explicit matcher**

Immediately after `_OPS_EX_RE`, add:

```python
_EXPLICIT_OPS_EX_RE = re.compile(
    r"\b(?:opex|op\s*ex|cam|tmi|operating\s+expenses?)\b"
    r"([^.!?\n$]{0,96}?)"
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
    r"(?:(?:/\s*|\bper\s+)(?:sf|psf|sq\.?\s*ft|square\s+foot))?"
    r"(?:\s*/?\s*(?:yr|year|annum|mo|month|monthly))?",
    re.IGNORECASE,
)

_RENT_REFERENCE_IN_OPS_EX_GAP_RE = re.compile(
    r"\b(?:asking\s+(?:rental\s+)?rate|base\s+rent|lease\s+rate|"
    r"rental\s+rate|asking\s+rent|rent\s+(?:is|at))\b",
    re.IGNORECASE,
)
```

The matcher deliberately excludes bare `NNN`: `NNN` is ambiguous between a lease basis and operating expenses, while the listed labels explicitly name OpEx.

- [ ] **Step 2: Evaluate explicit candidates after combined expressions and before the ambiguous matcher**

In `_extract_ops_ex_sf_from_text`, retain the existing `_COMBINED_RENT_OPEX_RE` block immediately after the initial empty/nonviable guards. After that combined block and before `matches = list(_OPS_EX_RE.finditer(text))`, add:

```python
    for explicit in _EXPLICIT_OPS_EX_RE.finditer(text):
        if _RENT_REFERENCE_IN_OPS_EX_GAP_RE.search(explicit.group(1)):
            continue
        if not _HYPOTHETICAL_RENT_RE.search(
            text[max(0, explicit.start() - 40): explicit.end()]
        ):
            value = float(explicit.group(2))
            window = text[
                max(0, explicit.start() - 15): min(len(text), explicit.end() + 25)
            ]
            annual = value * 12 if _is_monthly_context(window) else value
            if annual >= 0.01:
                return f"{annual:.2f}"
```

- [ ] **Step 3: Run the three production regressions and verify GREEN**

Run the Task 1 command again.

Expected: `3 passed`.

- [ ] **Step 4: Run focused extraction regressions**

Run:

```bash
E2E_TEST_MODE=true python3 -m pytest \
  tests/test_jill_live_campaign_regressions.py \
  tests/test_battery_ai_processing.py \
  tests/test_processing_completion_guards.py \
  tests/test_broker_language_broker_available_full_specs.py \
  tests/test_aprime_ai_processing.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the implementation**

```bash
git add email_automation/ai_processing.py
git commit -m "fix: prefer explicit opex figures"
```

### Task 3: Verify release safety and prepare an exact reviewed candidate

**Files:**
- Verify only: `email_automation/ai_processing.py`
- Verify only: `tests/test_jill_live_campaign_regressions.py`
- Verify only: `deploy-jill-one.sh`
- Verify only: `tests/test_process_user_production_deploy_contract.py`

- [ ] **Step 1: Run backend safety and deployment-contract suites**

Run the complete collected backend test surface:

```bash
E2E_TEST_MODE=true python3 -m pytest tests -q
```

Then rerun the release-critical contract files as an independently visible gate:

```bash
E2E_TEST_MODE=true python3 -m pytest -q \
  tests/test_process_user_production_deploy_contract.py \
  tests/test_runtime_dependency_contract.py \
  tests/test_ws_b_cloudrun_job_spec.py \
  tests/test_ws_b_cloudrun_service_spec.py \
  tests/test_ws_b_cutover_rollback_doc.py \
  tests/test_ws_b_dockerignore_contract.py \
  tests/test_ws_b_secret_coverage_contract.py \
  tests/test_ws_b_startup_env_validation.py
```

Expected: every collected test passes or is already explicitly skipped; both commands exit zero.

- [ ] **Step 2: Run syntax and repository integrity checks**

```bash
python3 -m py_compile email_automation/ai_processing.py tests/test_jill_live_campaign_regressions.py
git diff --check 8e250b81661af865f0cfab2bb8e6a75d42167463..HEAD
git status --short --branch
```

Expected: compilation and diff check succeed; status is clean on `fix/m1-opex-precedence-20260807`.

- [ ] **Step 3: Request independent review**

Ask the reviewer to inspect the exact candidate SHA for spec compliance, precedence safety, monthly/hypothetical preservation, recipient/access non-impact, and release-rail non-impact. Require the reviewer to rerun the two new regressions and focused extraction suites.

Expected: approval tied to one exact SHA, or actionable findings resolved and re-reviewed.

### Task 4: Deploy and prove the fix through production

**Files:**
- Update after evidence: `docs/release-safety/feature-gradebook.json` only if the repository's established release process requires it
- Update outside this public repository: the local Wave B grade and Brain checkpoint artifacts

- [ ] **Step 1: Capture rollback and safety readbacks**

Confirm campaign creation and automation are still globally closed, Baylor is the sole allowlisted UID, no process-user scheduler exists, the test campaign has follow-ups disabled, the outbox is empty, and the send cap has room for one new internal recipient message.

Expected: all controls remain closed/fail-safe before deployment.

- [ ] **Step 2: Deploy the exact reviewed SHA at zero traffic**

Build an immutable image tagged with the exact commit prefix, deploy a new Cloud Run revision with zero traffic using the unchanged release script/contracts, and verify image digest, environment, allowlists, cap, concurrency, readiness, and authenticated health.

Expected: candidate healthy at zero traffic and all readbacks equal the approved configuration.

- [ ] **Step 3: Promote and run a fresh browser canary**

Promote the reviewed revision to the Baylor-only production lane. Through `https://sitesiftai.com`, create a one-row campaign to `bp21harrison@gmail.com` with zero follow-ups. Reply from the signed-in broker mailbox with fresh wording containing an earlier `$X NNN` rent figure and a later explicit OpEx/CAM figure.

Expected: Gmail delivery succeeds; Firestore carries canonical inbound markers; the live Sheet contains distinct correct rent and OpEx values; Gross Rent matches `({Rent/SF/Yr} + {Ops Ex / SF}) * {Total SF} / 12`; zero duplicate actions, failures, dead letters, or unauthorized recipients occur.

- [ ] **Step 4: Record the checkpoint and retain rollback**

Record exact Git SHA, image digest, Cloud Run revision, frontend asset, campaign ID, Sheet evidence, Firestore evidence, and pass/fail result in the local grade and Brain checkpoint. Keep global creation/automation closed and retain the prior revision tag for rollback.
