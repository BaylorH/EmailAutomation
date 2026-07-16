# Broker Voice and Large Synthetic Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve automatic broker-reply voice without weakening campaign behavior, then grade a 22-row synthetic campaign without Firebase production or Microsoft Graph access.

**Architecture:** Keep the existing single structured OpenAI request and deterministic post-model guards. Extract the response-email policy into a testable prompt helper, update deterministic fallback copy through focused tests, and add a JSON-fixture dry-run harness that can run deterministically with canned proposals or against the real OpenAI model while production writes and sends are disabled.

**Tech Stack:** Python 3.12, unittest, OpenAI Responses API, existing SiteSift extraction/event guards, JSON fixtures, Firebase emulator environment.

---

## File Map

- Modify `email_automation/ai_processing.py`: extract and replace response-email prompt policy while leaving model and functional rules intact.
- Modify `email_automation/processing.py`: natural deterministic missing-field and terminal fallback builders.
- Create `tests/test_broker_voice_policy.py`: prompt and fallback contract tests.
- Create `tests/fixtures/broker_voice_large_campaign.json`: 22 synthetic rows and expected functional/tone properties.
- Create `tests/test_broker_voice_large_campaign.py`: fixture integrity and deterministic grader tests.
- Create `tests/run_broker_voice_large_campaign.py`: offline or real-model dry-run runner with production hard stops and evidence output.

### Task 1: Lock the existing functional contract

**Files:**
- Create: `tests/test_broker_voice_policy.py`

- [ ] **Step 1: Write failing prompt-contract tests**

Import `build_response_email_rules` and assert the returned policy contains these invariants: ask only authoritative missing fields; no Gross Rent request; no signature; `response_email` null for user-input, tour, redirect, call, and opt-out events; concrete acknowledgment; attachment awareness; concise natural voice. Assert it does not contain `PHRASE VARIATION RULES`, `rotate through these options`, or the Jill-observed canned transition menu.

- [ ] **Step 2: Run the policy test and verify RED**

Run:

```bash
GOOGLE_CLOUD_PROJECT=sitesift-test FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 .venv/bin/python -m unittest -v tests.test_broker_voice_policy
```

Expected: FAIL because `build_response_email_rules` does not exist.

- [ ] **Step 3: Extract the current policy without changing behavior**

Add `build_response_email_rules() -> str` in `ai_processing.py`, initially returning the current response rules, and call it from `propose_sheet_updates`.

- [ ] **Step 4: Run the test and confirm the intended RED assertions**

Run the same command. Expected: functional-invariant assertions pass while menu-removal and voice assertions fail.

- [ ] **Step 5: Commit the test seam**

```bash
git add email_automation/ai_processing.py tests/test_broker_voice_policy.py
git commit -m "test: lock broker response policy contract"
```

### Task 2: Replace phrase rotation with an experienced-broker voice policy

**Files:**
- Modify: `email_automation/ai_processing.py`
- Modify: `tests/test_broker_voice_policy.py`

- [ ] **Step 1: Add failing assertions for the complete policy**

Assert the policy explicitly requires one concrete acknowledgment when useful, prohibits re-asking supplied row/message/attachment facts, uses sentence form for one or two missing items and bullets for three or more, avoids fake enthusiasm and canned filler, and closes completed threads by reviewing with the client and welcoming other relevant fits.

- [ ] **Step 2: Run the policy test and verify RED**

Run the focused unittest command and confirm the new assertions fail on the old menu.

- [ ] **Step 3: Implement the compact voice policy**

Replace the phrase menu and example rotation blocks with one policy covering greeting/footer rules, attentive acknowledgment, missing-field precision, attachment awareness, sentence-versus-bullet formatting, complete-thread close, unavailable/alternative handling, and sensitive-event null responses. Do not modify `COLUMN_RULES`, `DOC_SELECTION_RULES`, `EVENT_RULES`, the output JSON schema, model `gpt-5.2`, temperature `0.1`, or deterministic augmenters.

- [ ] **Step 4: Run policy and safety regressions**

Run:

```bash
GOOGLE_CLOUD_PROJECT=sitesift-test FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 .venv/bin/python -m unittest -v tests.test_broker_voice_policy tests.test_processing_completion_guards tests.test_processing_reply_safety tests.test_processing_reply_identity tests.test_terminal_thread_processing
```

Expected: all tests pass.

- [ ] **Step 5: Commit the voice policy**

```bash
git add email_automation/ai_processing.py tests/test_broker_voice_policy.py
git commit -m "feat: give broker replies a natural attentive voice"
```

### Task 3: Natural deterministic fallbacks

**Files:**
- Modify: `tests/test_broker_voice_policy.py`
- Modify: `email_automation/processing.py`

- [ ] **Step 1: Write failing fallback tests**

Test `_build_missing_fields_response(contact_name, missing_fields)` and the existing `_select_automatic_response_body` fallbacks. Required behavior:

```python
body = _build_missing_fields_response("Alex Morgan", ["Docks", "Power"])
self.assertIn("Hi Alex,", body)
self.assertIn("docks", body.lower())
self.assertIn("power", body.lower())
self.assertNotIn("Thank you for the information!", body)
self.assertNotIn("To complete the property details", body)
self.assertNotIn("Best,", body)
```

The complete fallback must say the details will be reviewed with the client, invite questions or other relevant properties, and contain no exclamation mark.

- [ ] **Step 2: Run fallback tests and verify RED**

Run the focused policy unittest and confirm failure because the helper and new copy are absent.

- [ ] **Step 3: Implement minimal fallback builders**

Add `_build_missing_fields_response` that joins one or two fields naturally and uses bullets for three or more. Update unavailable, unavailable-with-alternative, and complete fallback copy to be warm and concise. Preserve `_response_mentions_missing_fields` as the functional eligibility gate.

- [ ] **Step 4: Run focused processing regressions**

Run:

```bash
GOOGLE_CLOUD_PROJECT=sitesift-test FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 .venv/bin/python -m unittest -v tests.test_broker_voice_policy tests.test_processing_completion_guards tests.test_processing_reply_safety
```

Expected: all tests pass.

- [ ] **Step 5: Commit fallback copy**

```bash
git add email_automation/processing.py tests/test_broker_voice_policy.py
git commit -m "feat: improve automatic reply fallbacks"
```

### Task 4: Define the 22-row Jill-derived campaign

**Files:**
- Create: `tests/fixtures/broker_voice_large_campaign.json`
- Create: `tests/test_broker_voice_large_campaign.py`

- [ ] **Step 1: Write a failing fixture-integrity test**

Require exactly 22 unique row IDs and cover these scenarios: partial details; complete details; one missing item; two missing items; four missing items; flyer attachment already supplied; floorplan attachment already supplied; drive-in count supplied; unavailable property; unavailable plus alternative; alternative with a new contact; wrong contact; explicit call request; tour offer; tour time reply; negotiation; client requirement question; client identity question; legal/LOI question; property issue; opt-out; ambiguous message.

Each fixture includes `row`, `column_config`, `conversation`, optional `pdf_manifest`, and `expect` keys for `event_types`, `response_mode`, `must_mention`, `must_not_mention`, and `max_words`.

- [ ] **Step 2: Run the fixture test and verify RED**

Run:

```bash
GOOGLE_CLOUD_PROJECT=sitesift-test FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 .venv/bin/python -m unittest -v tests.test_broker_voice_large_campaign
```

Expected: FAIL because the fixture file does not exist.

- [ ] **Step 3: Add the synthetic fixture deck**

Use synthetic properties and `broker+rowNN@example.test` recipients only. Include realistic industrial values and attachments as extracted text; never include customer identities, production UIDs, real mailbox addresses, or production campaign IDs.

- [ ] **Step 4: Run the fixture test and verify GREEN**

Run the same command and expect fixture count, uniqueness, coverage, and test-domain assertions to pass.

- [ ] **Step 5: Commit the campaign deck**

```bash
git add tests/fixtures/broker_voice_large_campaign.json tests/test_broker_voice_large_campaign.py
git commit -m "test: add 22-row broker voice campaign"
```

### Task 5: Deterministic campaign grader

**Files:**
- Modify: `tests/test_broker_voice_large_campaign.py`
- Create: `tests/run_broker_voice_large_campaign.py`

- [ ] **Step 1: Write failing grader tests**

Test `grade_case(case, proposal)` for event equality, required/forbidden terms, null-versus-send response mode, placeholders, signatures, repeated supplied-field asks, word count, excessive exclamation, and sensitive-event auto-response. Assert a clean proposal scores 100 and each deliberately broken proposal records a named veto.

- [ ] **Step 2: Run grader tests and verify RED**

Run the large-campaign unittest and confirm failure because the runner/grader does not exist.

- [ ] **Step 3: Implement offline and live-model modes**

The runner accepts `--mode offline` or `--mode live-model` and `--output <directory>`. It must exit before imports or calls unless:

```python
os.environ.get("FIRESTORE_EMULATOR_HOST")
and os.environ.get("GOOGLE_CLOUD_PROJECT") in {"sitesift-test", "demo-sitesift"}
and os.environ.get("SITESIFT_DISABLE_GRAPH_SENDS") == "1"
```

Offline mode grades fixture-owned proposals. Live-model mode calls `propose_sheet_updates(..., conversation=..., dry_run=True)` and never calls processing/send functions. Write `report.json` and `report.md` with per-row grades, vetoes, runtime, token usage when available, and aggregate score.

- [ ] **Step 4: Run offline proof and verify GREEN**

Run:

```bash
GOOGLE_CLOUD_PROJECT=sitesift-test FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 SITESIFT_DISABLE_GRAPH_SENDS=1 .venv/bin/python tests/run_broker_voice_large_campaign.py --mode offline --output /tmp/sitesift-broker-voice-offline
```

Expected: 22 rows graded, no production identifiers, no safety vetoes, and exit code 0.

- [ ] **Step 5: Commit the grader**

```bash
git add tests/run_broker_voice_large_campaign.py tests/test_broker_voice_large_campaign.py
git commit -m "test: grade large broker voice campaign"
```

### Task 6: Full non-production model proof

**Files:**
- Modify only if a failing fixture proves a scoped defect: `email_automation/ai_processing.py`, `email_automation/processing.py`, or the relevant tests.

- [ ] **Step 1: Run the 22-row real-model dry-run**

Run with the same hard-stop environment and `--mode live-model`, writing to `/Users/baylorharrison/Documents/SiteSiftEvidence/2026-07-15-broker-voice-admin-viewer-staging/backend`.

- [ ] **Step 2: Inspect vetoes before changing code**

Classify each failure as fixture error, prompt quality, deterministic guard, or model variance. Do not relax recipient, event, missing-field, placeholder, or terminal rules to improve tone scores.

- [ ] **Step 3: Fix one proven defect at a time with RED/GREEN tests**

For each genuine defect, add one minimal fixture or unittest, run it to observe the expected failure, implement the smallest correction, and rerun the focused test plus the 124-test safety baseline.

- [ ] **Step 4: Rerun the complete campaign and verification suite**

Run:

```bash
GOOGLE_CLOUD_PROJECT=sitesift-test FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 SITESIFT_DISABLE_GRAPH_SENDS=1 .venv/bin/python tests/run_broker_voice_large_campaign.py --mode live-model --output /Users/baylorharrison/Documents/SiteSiftEvidence/2026-07-15-broker-voice-admin-viewer-staging/backend
GOOGLE_CLOUD_PROJECT=sitesift-test FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 .venv/bin/python -m unittest -v tests.test_broker_voice_policy tests.test_broker_voice_large_campaign tests.test_processing_reply_safety tests.test_processing_completion_guards tests.test_terminal_thread_processing tests.test_processing_retryability tests.test_processing_reply_identity tests.test_broker_language_broker_available_partial_specs tests.test_broker_language_broker_available_full_specs tests.test_broker_language_broker_property_unavailable tests.test_broker_language_broker_tour_available tests.test_broker_language_broker_tour_unavailable
.venv/bin/python -m py_compile email_automation/ai_processing.py email_automation/processing.py tests/run_broker_voice_large_campaign.py
git diff --check
```

Expected: campaign has no safety vetoes, focused and baseline tests pass, compilation succeeds, and the diff is clean.

- [ ] **Step 5: Commit proven corrections and evidence metadata**

Stage only source, tests, fixtures, and a small evidence manifest that records commit hashes and report paths; do not commit API credentials or generated message bodies containing non-synthetic data.
