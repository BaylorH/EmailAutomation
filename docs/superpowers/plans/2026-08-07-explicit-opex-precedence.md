# Evidence-Bounded OpEx Precedence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the audited standalone CAM component without admitting combined rent-and-CAM totals or retaining matching model-proposed totals.

**Architecture:** Keep the existing combined component matcher first. Replace the arbitrary 96-character matcher with two narrow positive patterns, collect bounded combined-total evidence as numeric spans plus annualized values, and make both extraction and proposal validation consume that evidence. Compact OpEx forms remain owned by the legacy matcher.

**Tech Stack:** Python 3, `re`, `Decimal`, `unittest`, pytest, and the existing `email_automation.ai_processing` pipeline.

---

### Task 1: Lock the audited evidence matrix as RED tests

**Files:**
- Modify: `tests/test_jill_live_campaign_regressions.py`
- Test: `tests/test_jill_live_campaign_regressions.py`

- [ ] **Step 1: Add the combined-total, proposal, sequencing, uncertainty, and positive controls**

Add these methods to `JillLiveCampaignRegressionTests` while retaining the existing production extraction and proposal-overwrite regressions:

```python
    def test_combined_total_phrasings_are_not_opex(self):
        examples = (
            "CAM plus base rent equals $18.00 per square foot.",
            "CAM on top of base rent totals $18.00 per square foot.",
            "CAM in addition to base rent equals $18.00 per square foot.",
            "CAM and base rent total $18.00 per square foot.",
            "Base rent plus CAM equals $18.00 per square foot.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_rejected_combined_total_removes_model_opex_before_event_return(self):
        text = "CAM plus base rent equals $18.00 per square foot."
        proposal = {
            "updates": [{"column": "Ops Ex / SF", "value": "18.00"}],
            "events": [{"type": "property_unavailable", "reason": "leased"}],
        }
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertIsNone(ai_processing._proposal_update_for_column(result, "Ops Ex / SF"))

    def test_later_standalone_opex_wins_over_rejected_combined_total(self):
        text = (
            "CAM plus base rent totals $18.00/SF. "
            "CAM alone is $3.90/SF."
        )
        proposal = {"updates": [{"column": "Ops Ex / SF", "value": "18.00"}], "events": []}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertEqual(
            "3.90",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_pending_or_unknown_opex_does_not_capture_later_costs(self):
        examples = (
            "CAM is not finalized; rent is $14.10 per square foot.",
            "CAM is pending; the asking rate is $14.10 per square foot.",
            "CAM is unknown; total occupancy cost is $18.00 per square foot.",
            "CAM is pending; taxes are $3.90 per square foot.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_current_opex_wins_over_prior_figure(self):
        text = "Prior CAM was $4.25/SF; current CAM is $3.90/SF."
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_unresolved_projected_opex_range_is_not_extracted(self):
        text = "CAM is projected between $3.50 and $4.25 per square foot."
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_evidence_bounded_opex_positive_matrix(self):
        examples = {
            "CAM, on top of base rent, is $3.90 per square foot.": "3.90",
            "CAM (in addition to base rent) is $3.90 per square foot.": "3.90",
            "Rent is $14.10 and CAM is $3.90 per square foot.": "3.90",
            "2,000 SF: $1.25 NNN + $0.34 OPEX = $1.59 PSF / Month.": "4.08",
            "CAM is $0.34/SF/month.": "4.08",
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(expected, ai_processing._extract_ops_ex_sf_from_text(text))
```

Replace the prior empty-proposal combined-total write test with the preseeded,
event-bearing test above so the regression proves removal before the event early
return rather than merely proving deterministic extraction abstains.

- [ ] **Step 2: Run only the audit matrix and capture RED**

Run the existing production tests plus every method above with the repository
virtual environment and the standard emulator variables:

```bash
E2E_TEST_MODE=true \
FIRESTORE_EMULATOR_HOST=127.0.0.1:1 \
GOOGLE_APPLICATION_CREDENTIALS=/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json \
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python -m pytest -q \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_explicit_cam_figure_wins_over_earlier_nnn_rent_basis \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_explicit_cam_replaces_conflicting_model_opex_before_sheet_write \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_combined_total_phrasings_are_not_opex \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_rejected_combined_total_removes_model_opex_before_event_return \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_later_standalone_opex_wins_over_rejected_combined_total \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_pending_or_unknown_opex_does_not_capture_later_costs \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_current_opex_wins_over_prior_figure \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_unresolved_projected_opex_range_is_not_extracted \
  tests/test_jill_live_campaign_regressions.py::JillLiveCampaignRegressionTests::test_evidence_bounded_opex_positive_matrix
```

Expected: the production controls and already-safe table rows pass; broad-matcher
totals, preseeded-model retention, earlier-total precedence, prior/current, and
projected-range assertions fail for the audited reasons.

- [ ] **Step 3: Commit RED tests only**

```bash
git add tests/test_jill_live_campaign_regressions.py
git commit -m "test: cover evidence-bounded opex parsing"
```

### Task 2: Replace broad matching with bounded evidence

**Files:**
- Modify: `email_automation/ai_processing.py:1549-1905`
- Modify: `email_automation/ai_processing.py:3270-3420`
- Test: `tests/test_jill_live_campaign_regressions.py`

- [ ] **Step 1: Replace the broad matcher and relation guards**

Replace `_EXPLICIT_OPS_EX_RE`, `_RENT_ASSIGNMENT_IN_OPS_EX_GAP_RE`, and
`_OPS_EX_RELATION_TO_RENT_RE` with these bounded patterns:

```python
_OPS_EX_EXPLICIT_LABEL = r"(?:opex|op\s*ex|cam|tmi|operating\s+expenses?)"
_OPS_EX_DOLLAR_VALUE = (
    r"\$\s*(?P<value>[0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
    r"(?:(?:/\s*|\bper\s+)(?:sf|psf|sq\.?\s*ft|square\s+foot))?"
    r"(?:\s*/?\s*(?:yr|year|annum|mo|month|monthly))?"
)
_OPS_EX_COMPONENT_LIST_RE = re.compile(
    rf"\b{_OPS_EX_EXPLICIT_LABEL}\b"
    r"\s*,\s*(?:property\s+)?tax(?:es)?\s*,?\s*(?:and|&)\s+insurance\b"
    r"\s+(?:is|are)\s+"
    r"(?:(?:running|estimated)(?:\s+(?:roughly|approximately|about|around))?|"
    r"(?:roughly|approximately|about|around))\s*"
    rf"{_OPS_EX_DOLLAR_VALUE}",
    re.IGNORECASE,
)
_OPS_EX_RENT_MODIFIER = (
    r"(?:on\s+top\s+of|in\s+addition\s+to)\s+"
    r"(?:the\s+)?(?:base\s+)?rent"
)
_OPS_EX_RENT_MODIFIER_RE = re.compile(
    rf"\b{_OPS_EX_EXPLICIT_LABEL}\b\s*"
    rf"(?:,\s*{_OPS_EX_RENT_MODIFIER}\s*,|"
    rf"\(\s*{_OPS_EX_RENT_MODIFIER}\s*\))"
    rf"\s*(?:is|are)\s*{_OPS_EX_DOLLAR_VALUE}",
    re.IGNORECASE,
)
_COMBINED_TOTAL_RENT_LABEL = (
    r"(?:(?:base|asking)\s+rent|rent|lease\s+rate|rental\s+rate)"
)
_COMBINED_TOTAL_RELATION = (
    r"(?:plus|and|on\s+top\s+of|in\s+addition\s+to)"
)
_COMBINED_TOTAL_PREDICATE = (
    r"(?:(?:(?:combined(?:\s+total)?|all[-\s]?in|gross)"
    r"(?:\s+(?:rent|rate|cost))?\s+)?"
    r"(?:is|are|equals?|totals?|comes?\s+to|amounts?\s+to))"
)
_COMBINED_TOTAL_OPEX_RES = (
    re.compile(
        rf"\b{_OPS_EX_EXPLICIT_LABEL}\b\s+{_COMBINED_TOTAL_RELATION}\s+"
        rf"(?:the\s+)?{_COMBINED_TOTAL_RENT_LABEL}\b\s+"
        rf"{_COMBINED_TOTAL_PREDICATE}\s*{_OPS_EX_DOLLAR_VALUE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_COMBINED_TOTAL_RENT_LABEL}\b\s+{_COMBINED_TOTAL_RELATION}\s+"
        rf"{_OPS_EX_EXPLICIT_LABEL}\b\s+{_COMBINED_TOTAL_PREDICATE}\s*"
        rf"{_OPS_EX_DOLLAR_VALUE}",
        re.IGNORECASE,
    ),
)
```

- [ ] **Step 2: Add bounded evidence helpers**

Add these helper contracts next to the OpEx extractor:

```python
def _annualized_ops_ex_decimal(text: str, start: int, end: int, raw: str) -> Optional[Decimal]:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    window = text[max(0, start - 15):min(len(text), end + 25)]
    annual = value * Decimal("12") if _is_monthly_context(window) else value
    return annual if annual >= Decimal("0.01") else None


def _combined_total_opex_evidence(text: str) -> List[tuple]:
    evidence = []
    for pattern in _COMBINED_TOTAL_OPEX_RES:
        for match in pattern.finditer(text or ""):
            start, end = match.span("value")
            value = _annualized_ops_ex_decimal(text, match.start(), match.end(), match.group("value"))
            if value is not None:
                evidence.append((start, end, value))
    return evidence


def _ops_ex_standalone_candidates(text: str) -> List[tuple]:
    """Return non-rejected `(numeric_start, numeric_end, annual_value)` evidence."""
    text = text or ""
    rejected = _combined_total_opex_evidence(text)
    candidates = []

    def _append(match: "re.Match", group) -> None:
        if _HYPOTHETICAL_RENT_RE.search(
            text[max(0, match.start() - 40):match.end()]
        ):
            return
        start, end = match.span(group)
        if any(start < rejected_end and rejected_start < end
               for rejected_start, rejected_end, _ in rejected):
            return
        value = _annualized_ops_ex_decimal(
            text, match.start(), match.end(), match.group(group)
        )
        if value is not None:
            candidates.append((start, end, value))

    narrow_matches = sorted(
        [
            match
            for pattern in (_OPS_EX_COMPONENT_LIST_RE, _OPS_EX_RENT_MODIFIER_RE)
            for match in pattern.finditer(text)
        ],
        key=lambda match: match.start(),
    )
    for match in narrow_matches:
        _append(match, "value")

    legacy_matches = list(_OPS_EX_RE.finditer(text))
    legacy_matches.sort(key=lambda match: match.group(2) is None)
    for match in legacy_matches:
        if _opex_match_is_rent_basis_line(text, match):
            continue
        group = 1 if match.group(1) is not None else 2
        if match.group(group) is not None:
            _append(match, group)
    return candidates
```

- [ ] **Step 3: Make extraction consume the candidate collector**

Keep `_COMBINED_RENT_OPEX_RE` and its hypothetical/monthly handling first. Replace
the broad explicit loop and direct legacy loop with:

```python
    candidates = _ops_ex_standalone_candidates(text)
    if candidates:
        return f"{candidates[0][2]:.2f}"
    return None
```

- [ ] **Step 4: Strip only matching rejected model totals before early return**

Add a helper beside `_remove_proposal_update` that normalizes the existing OpEx
proposal, rejected total values, and valid standalone candidate values. Remove the
update only when its value is rejected and unsupported. Resolve `opex_col` near
`rent_col` and `total_sf_col`, call the helper after existing rent/area proposal
validation and before the event-type early return, and pass the same `opex_col` to
`_fill`.

```python
def _strip_rejected_combined_total_opex_update(
    proposal: dict,
    opex_col: Optional[str],
    text: str,
) -> None:
    update = _proposal_update_for_column(proposal, opex_col) if opex_col else None
    if update is None:
        return
    proposed = _normalized_numeric_value(update.get("value"))
    rejected_values = {
        value for _, _, value in _combined_total_opex_evidence(text)
    }
    supported_values = {
        value for _, _, value in _ops_ex_standalone_candidates(text)
    }
    if proposed in rejected_values and proposed not in supported_values:
        _remove_proposal_update(proposal, opex_col)
```

- [ ] **Step 5: Run targeted GREEN**

Rerun the exact Task 1 command. Expected: every selected test and subtest passes.

- [ ] **Step 6: Commit production code only**

```bash
git add email_automation/ai_processing.py
git commit -m "fix: bound explicit opex evidence"
```

### Task 3: Verify the focused backend surface

**Files:**
- Verify: `email_automation/ai_processing.py`
- Verify: `tests/test_jill_live_campaign_regressions.py`

- [ ] **Step 1: Run the focused five-file suite**

```bash
E2E_TEST_MODE=true \
FIRESTORE_EMULATOR_HOST=127.0.0.1:1 \
GOOGLE_APPLICATION_CREDENTIALS=/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json \
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python -m pytest -q \
  tests/test_jill_live_campaign_regressions.py \
  tests/test_battery_ai_processing.py \
  tests/test_processing_completion_guards.py \
  tests/test_broker_language_broker_available_full_specs.py \
  tests/test_aprime_ai_processing.py
```

Expected: every collected test and subtest passes; only already-known dependency
deprecation warnings may remain.

- [ ] **Step 2: Run syntax and repository checks**

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python -m py_compile \
  email_automation/ai_processing.py tests/test_jill_live_campaign_regressions.py
git diff --check
git status --short --branch
```

Expected: compilation and diff checks exit zero and the worktree is clean after
the production commit. Do not push or deploy from this plan.
