# E2E Master Test Plan - Production Readiness

## Overview

This is the **master plan** for achieving production readiness through comprehensive E2E testing. It includes feature coverage mapping, bug-fixing phases, and a final clean sweep requirement.

**Goal:** Complete all tests with ZERO failures before production deployment.

---

## Feature Coverage Analysis

### Backend Features (50+) vs E2E Test Coverage

| Category | Total Features | Round 1 Coverage | Round 2 Coverage | Gap |
|----------|----------------|------------------|------------------|-----|
| **Event Types** | 9 | 6 (67%) | 3 (33%) | ✅ 100% |
| **Notification Types** | 5 | 5 (100%) | 0 | ✅ 100% |
| **Sheet Operations** | 3 | 3 (100%) | 0 | ✅ 100% |
| **Email Operations** | 7 | 5 (71%) | 1 | ⚠️ 86% |
| **Column Handling** | 6 | 4 (67%) | 0 | ⚠️ 67% |
| **Edge Cases** | 6 | 4 (67%) | 2 (33%) | ✅ 100% |
| **Follow-up System** | 5 | 0 | 1 (20%) | ⚠️ 20% |

### Event Type Coverage

| Event | Tested In | Status |
|-------|-----------|--------|
| `property_unavailable` | Round 1: Scenario C | ✅ |
| `new_property` | Round 1: Scenario C | ✅ |
| `call_requested` | Round 2: Scenario H | ✅ |
| `tour_requested` | Round 1: Scenario F | ✅ |
| `close_conversation` | Round 2: Scenario L | ✅ |
| `needs_user_input` | Round 1: Scenario E | ✅ |
| `contact_optout` | Round 1: Scenario G | ✅ |
| `wrong_contact` | Round 2: Scenario I | ✅ |
| `property_issue` | Round 2: Scenario J | ✅ |

### Frontend Features vs E2E Test Coverage

| Component | Tested | Notes |
|-----------|--------|-------|
| **AddClientModal** | ✅ | Round 1 Phase 1 |
| **ColumnMappingStep** | ✅ | Round 1 Phase 1 |
| **StartProjectModal** | ✅ | Round 1 Phase 1 |
| **NewPropertyRequestModal** | ✅ | Round 1 Scenarios C, E, F |
| **NotificationsSidebar** | ✅ | All scenarios |
| **ConversationsModal** | ✅ | Round 1 Phase 3 |
| **Dashboard Stats** | ⚠️ | Partially (visual only) |
| **Real-time Notifications** | ✅ | Round 1 added |
| **ChatWithAI** | ✅ | Round 1 Scenario E |
| **BlockedContactsModal** | ⚠️ | Not explicitly tested |
| **SettingsPage** | ❌ | Not in scope |
| **Email Signature** | ⚠️ | Implicit (in sent emails) |

### Gaps Identified

| Gap | Risk | Resolution |
|-----|------|------------|
| Follow-up system only 20% tested | MEDIUM | Round 2 Scenario K tests scheduling, but not full cycle |
| BlockedContactsModal not tested | LOW | Add to Round 2 post-optout |
| Dashboard stats not validated | LOW | Add explicit verification step |
| Email signature in sent emails | LOW | Add verification in Phase 3 |
| Formula column protection | HIGH | Already in Round 1 but add explicit check |

---

## Master Execution Plan

### Phase Structure

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           MASTER PLAN PHASES                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  PHASE 0: Baseline & Preparation                                        │
│    └─> Verify all tools, snapshot state, confirm ready                  │
│                                                                          │
│  PHASE 1: Round 1 E2E Test (7 scenarios, 6 properties)                  │
│    └─> Execute A-G scenarios, document all issues                       │
│                                                                          │
│  PHASE 2: Round 1 Bug Fix Sprint                                        │
│    └─> Fix all issues found in Round 1                                  │
│    └─> Re-test failed scenarios only                                    │
│                                                                          │
│  PHASE 3: Round 2 E2E Test (5 scenarios, 5 properties)                  │
│    └─> Execute H-L scenarios, document all issues                       │
│                                                                          │
│  PHASE 4: Round 2 Bug Fix Sprint                                        │
│    └─> Fix all issues found in Round 2                                  │
│    └─> Re-test failed scenarios only                                    │
│                                                                          │
│  PHASE 5: Clean Sweep Test (FULL PASS REQUIRED)                         │
│    └─> Fresh client, run ALL scenarios A-L                              │
│    └─> Must pass 100% to proceed                                        │
│                                                                          │
│  PHASE 6: Production Readiness Sign-off                                 │
│    └─> Final report, checklist, approval                                │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## PHASE 0: Baseline & Preparation

### 0.1 Tool Verification
🤖 **CLAUDE:**
- [ ] Firestore access confirmed
- [ ] Google Sheets access confirmed
- [ ] Microsoft Graph API access confirmed
- [ ] Both repos up to date (git status clean)

### 0.2 Test Files Ready
- [ ] `Scrub Augusta GA.xlsx` - Round 1 (5 properties)
- [ ] `Scrub Augusta GA - Round 2.xlsx` - Round 2 (5 properties)
- [ ] `E2E_EXECUTION_PLAN.md` - Round 1 scripts
- [ ] `E2E_ROUND2_SCRIPTS.md` - Round 2 scripts

### 0.3 Clean State
- [ ] Delete any existing test clients
- [ ] Outbox is empty
- [ ] No pending notifications from previous tests

---

## PHASE 1: Round 1 E2E Test

**File:** `Scrub Augusta GA.xlsx`
**Properties:** 5 (rows 3-7)
**Scenarios:** A-G (7 scenarios)

### Execution Sequence

| Step | Scenario | Property | What We Test |
|------|----------|----------|--------------|
| 1.1-1.6 | Setup | All | Campaign launch, outbox, email send |
| A | Complete Info | 699 Industrial Park Dr | 1-turn extraction + voluntary rent |
| B | Partial→Complete | 135 Trade Center Court | 2-turn multi-field extraction |
| C | Unavailable+New | 2058 Gordon Hwy | NON-VIABLE move + new property |
| D | Long Conversation | 1 Kuhlke Dr | 5+ turn thread integrity |
| E | Escalation | 1 Randolph Ct | needs_user_input + ChatWithAI |
| F | Tour Requested | 500 Bobby Jones | tour_requested event + modal |
| G | Contact Optout | 699 Industrial Park Dr | contact_optout + blocked list |

### Issue Tracking Template

```markdown
## Round 1 Issues Found

### Issue #1: [Title]
- **Scenario:** [A-G]
- **Severity:** [Critical/High/Medium/Low]
- **Description:** [What happened]
- **Expected:** [What should have happened]
- **Actual:** [What actually happened]
- **Files Affected:** [List files]
- **Fix Status:** [ ] Not started / [ ] In progress / [ ] Fixed / [ ] Verified
```

### Round 1 Checkpoint
⏸️ After completing all Round 1 scenarios:

🤖 **CLAUDE GENERATES:**
- Issue list with severity
- Recommendation: Fix now vs defer
- Estimated fix count

🧑 **USER DECIDES:**
- Proceed to Phase 2 (bug fixes)
- Skip to Round 2 if no critical issues

---

## PHASE 2: Round 1 Bug Fix Sprint

### 2.1 Triage
🤖 **CLAUDE:**
- Categorize issues by severity
- Identify root causes
- Propose fix order (critical → high → medium)

### 2.2 Fix Implementation
For each issue:
1. 🤖 Claude proposes fix
2. 🧑 User approves
3. 🤖 Claude implements
4. 🤖 Claude runs relevant unit tests (if available)
5. Commit with issue reference

### 2.3 Targeted Re-test
Only re-run failed scenarios:
- If Scenario A failed → Re-test Scenario A only
- If Scenario C failed → Re-test Scenario C only
- Verify fix worked

### 2.4 Fix Confirmation
- [ ] All critical issues fixed
- [ ] All high issues fixed
- [ ] Medium/low issues documented for later

---

## PHASE 3: Round 2 E2E Test

**File:** `Scrub Augusta GA - Round 2.xlsx`
**Properties:** 5 (rows 3-7)
**Scenarios:** H-L (5 scenarios)

### Execution Sequence

| Step | Scenario | Property | What We Test |
|------|----------|----------|--------------|
| Setup | All | All | New client creation, campaign launch |
| H | Call Requested | 250 Peach Orchard Rd | call_requested event + phone capture |
| I | Wrong Contact | 1500 Walton Way | wrong_contact:forwarded + new contact flow |
| J | Property Issue | 3200 Washington Rd | property_issue:major + notes |
| K | Non-Responsive | 450 Broad St | Follow-up scheduling (time permitting) |
| L | Natural Close | 800 Reynolds St | close_conversation + thread termination |

### Additional Round 2 Checks
- [ ] BlockedContactsModal after Scenario G optout
- [ ] Dashboard stats accuracy
- [ ] Email signature appears in all sent emails

---

## PHASE 4: Round 2 Bug Fix Sprint

Same process as Phase 2:
1. Triage issues
2. Fix in priority order
3. Targeted re-test
4. Confirm fixes

---

## PHASE 5: Clean Sweep Test (MANDATORY)

### Purpose
Run ALL scenarios on a FRESH client to confirm no regressions.

### Requirements
- **MUST pass 100%** to proceed to production
- Uses combined test file (all 10 properties) OR
- Runs Round 1 + Round 2 sequentially on fresh clients

### Clean Sweep Execution

🧑 **USER:**
1. Create fresh client with Round 1 file
2. Execute ALL Round 1 scenarios (A-G)
3. Create fresh client with Round 2 file (or continue)
4. Execute ALL Round 2 scenarios (H-L)

🤖 **CLAUDE:**
- Verify each scenario passes
- No re-testing individual scenarios
- Any failure = STOP and fix

### Clean Sweep Criteria

| Criteria | Required | Status |
|----------|----------|--------|
| All 12 scenarios pass | YES | [ ] |
| No data corruption | YES | [ ] |
| All modals work correctly | YES | [ ] |
| All notifications accurate | YES | [ ] |
| Threading maintained | YES | [ ] |
| Forbidden fields protected | YES | [ ] |
| Real-time updates work | YES | [ ] |

### If Clean Sweep Fails
1. Document failure
2. Return to bug fix sprint
3. Fix issue
4. Re-run ENTIRE clean sweep (not just failed scenario)
5. Repeat until 100% pass

---

## PHASE 6: Production Readiness Sign-off

### Final Checklist

**Core Functionality:**
- [ ] Campaign launch works end-to-end
- [ ] Email sending/receiving works
- [ ] Thread matching works (message ID and conversation ID)
- [ ] AI extraction works for all field types
- [ ] Multi-turn conversations work (2-turn, 5+ turn)
- [ ] Property completion detection works

**Event Handling:**
- [ ] property_unavailable → NON-VIABLE move
- [ ] new_property → approval flow → new row
- [ ] call_requested → escalation
- [ ] tour_requested → suggested response
- [ ] close_conversation → thread termination
- [ ] needs_user_input → pause/resume
- [ ] contact_optout → blocked list
- [ ] wrong_contact → redirect info
- [ ] property_issue → severity notification

**Data Integrity:**
- [ ] Row numbers stay in sync
- [ ] Leasing Contact never overwritten
- [ ] Rent captured when volunteered, never requested
- [ ] Gross Rent never written (formula column)
- [ ] Formula columns protected

**UI/UX:**
- [ ] All modals close immediately (no waiting)
- [ ] Notifications appear in real-time
- [ ] ChatWithAI works for response composition
- [ ] Conversations viewable in modal

**Error Handling:**
- [ ] No orphaned threads
- [ ] No duplicate emails
- [ ] Pending responses retry works
- [ ] Dead letter queue captures failures

### Final Report

🤖 **CLAUDE GENERATES:**

```markdown
# E2E Test Final Report - [DATE]

## Executive Summary
- **Status:** PRODUCTION READY / NOT READY
- **Test Duration:** [X hours/days]
- **Total Scenarios:** 12
- **Pass Rate:** [X]%
- **Issues Found & Fixed:** [X]
- **Remaining Issues:** [X] (severity breakdown)

## Round 1 Results
| Scenario | Status | Issues | Fixed |
|----------|--------|--------|-------|
| A-G      | ✅/❌  | X      | X     |

## Round 2 Results
| Scenario | Status | Issues | Fixed |
|----------|--------|--------|-------|
| H-L      | ✅/❌  | X      | X     |

## Clean Sweep Results
| Attempt | Date | Result |
|---------|------|--------|
| 1       | X    | ✅/❌  |
| 2 (if needed) | X | ✅/❌ |

## Issues Fixed (Summary)
1. [Issue description + fix]
2. ...

## Known Issues (Deferred)
1. [Low-priority issue + rationale for deferral]

## Feature Coverage
- Events: 9/9 ✅
- Notifications: 5/5 ✅
- Modals: 8/8 ✅
- Edge Cases: 6/6 ✅

## Recommendation
[APPROVE FOR PRODUCTION / REQUIRES ADDITIONAL WORK]

## Sign-off
- [ ] Claude confirms all tests passed
- [ ] User confirms system behaves as expected
- [ ] Ready for production deployment
```

---

## Quick Reference

### Test Files
| File | Properties | Scenarios |
|------|------------|-----------|
| `Scrub Augusta GA.xlsx` | 5 | A-G (Round 1) |
| `Scrub Augusta GA - Round 2.xlsx` | 5 | H-L (Round 2) |

### Email Accounts
| Account | Role |
|---------|------|
| baylor.freelance@outlook.com | System (outbound) |
| bp21harrison@gmail.com | Broker simulator |
| baylor@manifoldengineering.ai | Broker simulator |

### Key Documents
| Document | Purpose |
|----------|---------|
| `E2E_EXECUTION_PLAN.md` | Round 1 step-by-step |
| `E2E_CONVERSATION_SCRIPTS.md` | Round 1 broker replies |
| `E2E_ROUND2_SCRIPTS.md` | Round 2 broker replies |
| `E2E_MASTER_PLAN.md` | This document (master plan) |
| `E2E_TEST_PLAN.md` | Success criteria rubric |

---

## Time Estimates (Rough)

| Phase | Estimated Duration |
|-------|-------------------|
| Phase 0: Baseline | 10 min |
| Phase 1: Round 1 | 60-90 min |
| Phase 2: Bug Fixes | Variable (0-120 min) |
| Phase 3: Round 2 | 45-60 min |
| Phase 4: Bug Fixes | Variable (0-60 min) |
| Phase 5: Clean Sweep | 90-120 min |
| Phase 6: Sign-off | 15 min |
| **Total** | **4-8 hours** |

---

## Success Criteria

**PRODUCTION READY when:**
1. ✅ Clean sweep passes 100%
2. ✅ All 9 event types tested and working
3. ✅ All 5 notification types working
4. ✅ All modals close immediately
5. ✅ Real-time notifications work
6. ✅ No critical or high severity bugs remaining
7. ✅ Data integrity maintained throughout

**NOT READY if:**
- ❌ Any clean sweep scenario fails
- ❌ Critical bug not fixed
- ❌ Data corruption detected
- ❌ Threading breaks down
- ❌ Forbidden fields written
