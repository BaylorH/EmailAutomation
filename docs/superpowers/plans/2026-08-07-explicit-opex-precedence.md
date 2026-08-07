# Evidence-Bounded OpEx Precedence Implementation Plan

> **Execution mode:** local no-ship TDD. Do not push, deploy, or contact external
> systems or people.

**Goal:** Classify explicit OpEx evidence without duplicating rent, bind complete
rate suffixes and basis only to the winning candidate, and prevent rejected,
rent-owned, or explicitly negated figures from surviving as unsupported
model-proposed writes.

**Deliverable:** both code and verified findings.

**Architecture:** Build immutable `_OpsExCandidate` records containing raw and
annualized `Decimal` values, basis, spans, source, and precedence. Select one
winner for both extraction and proposal normalization. Resolve field ownership
once, including the bounded pending/TBD override and bounded correction or
pronominal inheritance, and retain the complete per-area rate tail with optional
basis and `NNN` in either bounded order. Capture a correction's entire current
rate tail as `current_evidence` and resolve basis only from that span. Keep
rejected combined totals, combined-equation base-rent figures, non-expense NNN
figures, and explicitly negated rates in separate negative-evidence collections
so symmetric negated-rate sanitation removes only values without independent
destination-field support.

**Tech stack:** Python 3, `re`, `Decimal`, `NamedTuple`, `unittest`, pytest, and
the existing `email_automation.ai_processing` pipeline.

---

## Task 1: Lock the consolidated behavior as RED

**Files:**

- Modify: `tests/test_jill_live_campaign_regressions.py`

- [x] Add rent-only offer/offered/offering, available/availability, and area NNN
  cases using word `at`, `for`, `@`, colon, and typographic dash separators.
  Assert rent `14.10`, OpEx `None`, and no matching OpEx update after full
  augmentation from either an empty or preseeded proposal.
- [x] Add pending/TBD expense-owner cases followed by a later Availability/offer
  governor, independently covering `|`, `:`, ASCII hyphen, en dash, and em dash,
  with direct `CAM:` and semicolon, period, and newline controls.
- [x] Preserve explicit expense-owned `$3.65 NNN`, CAM, OpEx, and TMI forms.
  Assert bare NNN is neutral, immediate `/CAM|/OpEx|/TMI` owns the figure as
  expense, and conflicting asking-plus-expense ownership abstains.
- [x] Add explicit-expense `per SF NNN` and `per square foot NNN` cases plus
  composed `per SF/month NNN`, `per SF per month NNN`, and `per SF/year NNN`
  cases. Assert Rent/OpEx extraction and proposal parity, correct annualization,
  and no Rent duplication.
- [x] Add a standalone long-form `Expenses ... per SF NNN, billed monthly`
  extraction control that preserves NNN-first basis ownership and annualization.
- [x] Add combined equations using `/SF/month`, `per SF/month`, `per-SF/month`,
  `per sq. ft., billed monthly`, and `per square foot, billed monthly`. Assert
  direct extraction and raw proposal normalization both produce `4.08`.
- [x] Add standalone `square foot`, `sq ft.`, and `sq ft` monthly variants,
  including `billed on a|the monthly basis`.
- [x] Add contamination negatives for `monthly-report`, `monthly - rent`,
  `monthly: rent`, and `monthly (rent ...)`, plus working clause-boundary and
  parking controls.
- [x] Add rejected-total tests proving raw `1.50` and annualized `18.00` are
  removed before a terminal event despite unrelated annual parking or a
  `/month/year` conflict.
- [x] Add qualified rent-owner, explicit expense-plus-area, expense-owned
  `/SF NNN`, reversed tax/insurance order, competing rent-subject, relational
  governing-subject, bounded-keyword, correction-recency, and symmetric
  preseeded-write controls in separate RED commits.
- [x] Add bounded elliptical OpEx and pronominal Rent/OpEx correction matrices,
  including full suffix retention, current-figure-only basis changes,
  negated-rate sanitation, unrelated later-rate controls, repeated
  normalization, and extraction/proposal parity.
- [x] Add focused NNN-first correction cases covering prior
  `NNN, billed monthly` tails in elliptical and pronominal forms and current
  `NNN/month` plus `NNN, billed monthly` tails in elliptical forms. Assert
  extraction/proposal parity and repeated normalization.
- [x] Run the focused matrix against the pre-fix production code and capture the
  expected failures.
- [x] Commit tests separately as `test: consolidate opex ownership edge cases`.

## Task 2: Implement shared positive and negative evidence

**Files:**

- Modify: `email_automation/ai_processing.py`

- [x] Keep `_OpsExCandidate` as the sole accepted record and `_ops_ex_winner` as
  the sole selector consumed by extraction and proposal basis normalization.
- [x] Add `_nnn_figure_owner` for explicit `rent|opex|neutral|conflict`
  classification without magnitude inference. Reuse it in rent extraction,
  OpEx candidate admission, and rejected-NNN evidence.
- [x] Generalize ownership through `_figure_field_owner`: ignore relational
  objects, select the nearest figure-governing explicit subject, let explicit
  fields ordinarily outrank contextual offer/area syntax, and allow pending/TBD
  expense ownership to yield to a later Availability/offer governor only across
  the captured structural separators. Preserve direct CAM ownership, natural
  clause boundaries, and conflict for direct same-figure co-ownership.
- [x] Route explicit-unit rent and legacy OpEx admission through the shared
  owner, require true rent-keyword boundaries, and remove preseeded cross-field
  values unless independently supported by their destination field, including
  raw and annualized values from explicitly monthly rent.
- [x] Consume combined equation totals and rate units in the combined matcher so
  the existing 10-before/30-after basis window reaches its owned suffix.
- [x] Extend shared rate-suffix handling for `per-SF`, punctuation in `sq ft.`,
  `billed on a|the monthly basis`, and complete per-area tails with optional
  basis and `NNN` in either bounded order, including `NNN/month` and
  `NNN, billed monthly`.
- [x] Reject basis markers followed by hyphenated, colon-delimited,
  parenthetical, or multiword competing subjects while preserving real clause
  boundaries, attached monthly syntax, and coordinated supporting
  tax-plus-insurance qualifiers in either order on explicit OpEx rates.
- [x] Treat correction/corrected/correct/actually as current evidence; bind only
  the captured elliptical and `not $old...; it is $new...` forms to their prior
  field, retain each figure's complete rate tail in either bounded order, capture
  the whole current tail as `current_evidence`, and resolve basis only from that
  span. Keep unrelated later rates separate and select the latest equally
  specific candidate across sequential corrections.
- [x] Preserve combined-total raw negative evidence before basis resolution. On
  conflict, reject raw plus x12 whenever the owned basis context contains a
  monthly marker, independent of token order or partial outer-regex capture.
- [x] Exclude immediately negated rates from both extractors and include their
  raw/annual values in symmetric Rent and OpEx proposal sanitation. Include
  rejected NNN, combined-total, and combined-equation base-rent raw/annual
  values in OpEx validation. Never remove a value independently supported by
  its destination field.
- [x] Run the consolidated matrix and full Jill file green before documentation.

## Task 3: Refresh architecture documentation

**Files:**

- Modify: `docs/superpowers/specs/2026-08-07-explicit-opex-precedence-design.md`
- Modify: `docs/superpowers/plans/2026-08-07-explicit-opex-precedence.md`

- [x] Replace the superseded narrow-candidate-only description with the shared
  candidate/winner architecture.
- [x] Document field-owned basis, complete two-order rate tails, bounded pending
  and correction inheritance, whole-current-evidence correction basis, ambiguous
  NNN classification, negated negative evidence, symmetric proposal sanitation,
  proposal idempotency, and no-ship boundaries.
- [x] Remove stale pseudocode and acceptance statements that conflict with the
  implemented record shape or proposal flow.

## Task 4: Verify and commit

- [x] Run full Jill regressions.
- [x] Run the exact focused five-file backend suite.
- [x] Run the exact release-critical selection:

```bash
E2E_TEST_MODE=true \
FIRESTORE_EMULATOR_HOST=127.0.0.1:1 \
GOOGLE_APPLICATION_CREDENTIALS=/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json \
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python -m pytest -q \
  tests/test_process_user_production_deploy_contract.py \
  tests/test_runtime_dependency_contract.py \
  tests/test_ws_b_cloudrun_job_spec.py \
  tests/test_ws_b_cloudrun_service_spec.py \
  tests/test_ws_b_cutover_rollback_doc.py \
  tests/test_ws_b_dockerignore_contract.py \
  tests/test_ws_b_secret_coverage_contract.py \
  tests/test_ws_b_startup_env_validation.py
```

- [x] Run syntax and repository checks:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python -m py_compile \
  email_automation/ai_processing.py tests/test_jill_live_campaign_regressions.py
git diff --check
git status --short --branch
```

- [x] Commit production code and refreshed docs separately from the RED test
  commit. Keep the worktree clean. Do not push or deploy.
