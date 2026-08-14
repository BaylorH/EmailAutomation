# Outbound Dash Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent generated clause dashes such as `needed—I’ll` from becoming the visibly jammed outbound text `needed-I'll`.

**Architecture:** Keep the existing ASCII outbound transport boundary in `normalize_outbound_message_text`. Replace clause-level en/em dashes with a single spaced ASCII hyphen before applying the existing smart punctuation translation; preserve ordinary intra-word hyphens and already-spaced dashes. This patch does not change extraction, response selection, signatures, recipients, or provider behavior.

**Tech Stack:** Python 3.12, pytest, existing `email_automation.utils` outbound formatter.

---

### Task 1: Normalize clause dashes without changing ordinary hyphens

**Files:**
- Modify: `email_automation/utils.py:35-64`
- Test: `tests/test_signature_footer.py`

- [ ] **Step 1: Write the failing regression test**

Import `normalize_outbound_message_text`, then add a `unittest` method covering unspaced em/en dashes, an already-spaced dash, and an ordinary intra-word hyphen:

```python
def test_outbound_clause_dashes_remain_readable(self):
    cases = [
        ("Everything I needed—I’ll follow up.", "Everything I needed - I'll follow up."),
        ("Everything I needed–I’ll follow up.", "Everything I needed - I'll follow up."),
        ("Everything I needed — I’ll follow up.", "Everything I needed - I'll follow up."),
        ("A well-known property", "A well-known property"),
    ]
    for source, expected in cases:
        with self.subTest(source=source):
            self.assertEqual(expected, normalize_outbound_message_text(source))
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
E2E_TEST_MODE=true FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
PYTHONDONTWRITEBYTECODE=1 \
/Users/baylorharrison/.config/superpowers/worktrees/EmailAutomation/policy-blocked-reply-review-release-20260812/.venv/bin/python \
-m pytest -q -p no:cacheprovider \
tests/test_signature_footer.py::SignatureFooterTests::test_outbound_clause_dashes_remain_readable
```

Expected: three clause-dash cases fail because the current translation produces unspaced or double-spaced ASCII hyphens; the ordinary hyphen case passes.

- [ ] **Step 3: Implement the minimal normalization**

In `normalize_outbound_message_text`, normalize any optional horizontal whitespace surrounding `\u2013`, `\u2014`, or `\u2015` to exactly `" - "`, then apply `_OUTBOUND_TEXT_TRANSLATION` for quotes and remaining punctuation.

- [ ] **Step 4: Run focused verification**

Run the regression test, then:

```bash
E2E_TEST_MODE=true FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
PYTHONDONTWRITEBYTECODE=1 \
/Users/baylorharrison/.config/superpowers/worktrees/EmailAutomation/policy-blocked-reply-review-release-20260812/.venv/bin/python \
-m pytest -q -p no:cacheprovider \
tests/test_signature_footer.py tests/test_processing_completion_guards.py
```

Expected: 84 focused tests plus the new regression pass; no provider or mailbox call occurs.

- [ ] **Step 5: Review, commit, publish, and deploy the exact candidate**

Require a clean diff limited to the plan, one test file, and `email_automation/utils.py`; run `git diff --check`; commit with the observed RED/GREEN evidence. Publish the branch, deploy only the exact reviewed SHA with both global switches remaining closed, then verify that deployed revision in the operations dashboard.

- [ ] **Step 6: Run one self-owned browser proof**

Use signed-in sender `baylor.freelance@outlook.com` and only the explicitly authorized recipient `baylor@manifoldengineering.ai`. Exercise one complete-information conversation, inspect the reply and dashboard state, and require: exact extracted values/units, no repeated question, readable punctuation, no cross-property effect, one terminal transition, and no additional recipient.
